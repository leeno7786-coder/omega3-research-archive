# Semantic Retina–Binary Memory Co-Design
## A Hermetic Neural-to-Binary Front End for a Persistent, Neuromodulated AI Memory Organ

**Status:** Research paper draft v0.4 — operator seat amendment applied; proceed-to-experiments research state, not an architecture freeze
**Date:** 2026-08-16
**Project:** Omega Memory Organ / Omega AI LLC
**Review state:** second Fable review at `1dec7d80`; ADR-MO-018 subsequently supersedes its runtime-seat assumption and fixes the semantic retina brain-side with no transport fallback

---

> **Operator amendment — 2026-08-16 / ADR-MO-018:** All earlier language in this paper or its Fable reviews that requires the semantic retina to live in `omega-runtime`, behind an endpoint, socket, IPC seam, or runtime worker is superseded. The retina is a bounded **brain-resident memory appliance** with an in-process call and **no transport/runtime fallback**. The broader runtime-owns-general-models rule remains in force for other model classes.

## Abstract

Persistent AI memory systems commonly use dense neural embeddings as both semantic representations and retrieval keys. In Omega's prior memory stack, that choice created a synchronization and reliability bottleneck: semantic writes depended on a resident embedding service, failed embeddings entered a backfill queue, projection/reindex work drained the same dependency, and semantic recall required a new query embedding before vector similarity could run. The learned encoder therefore sat in the middle of both write-time and recall-time memory mechanics, serializing otherwise parallel lanes and creating a second class of partially projected memories.

This paper proposes a different division of labor. A small frozen transformer is retained only as a resident **semantic sensory transducer**. It is quantized with AMD Quark and exported as a version-pinned ONNX graph whose canonical output is a 256-dimensional INT8 latent vector. That vector crosses a hard boundary into deterministic memory mechanics through Hyperplane Quantized Neural-to-Binary Projection (HQ-NP): a fixed sign-projection maps the transient semantic latent into a 16,384-bit hypervector. The dense latent is never persisted, indexed, searched, reinforced, or treated as evidence. All persistent memory semantics remain in Omega's canonical filesystem, structural graph, DME binary substrate, verifier, and adaptive sidecars.

The frozen semantic prior is then combined with a separate **target neuromodulated plasticity system**. In the proposed end state, exact execution traces create eligibility, verified actual use gates learning, episode outcome produces reward-prediction error and modulatory state, and DeepSleep owns potentiation, depression, decay, dormancy, and learned-plane updates. The live code does not yet fully satisfy that topology; v0.2 treats the existing cascade-time reinforcement path as a violation-to-fix before developmental experiments. Thus the front end remains fixed while the memory organ grows through lived experience. The design is explicitly hardware/software co-designed for Omega's XDNA 2 + IRON/MLIR-AIE execution substrate, with portable reference semantics used as a correctness oracle rather than as a least-common-denominator architecture.


---

## 1. Introduction

The target problem is not merely semantic search. Omega requires a persistent memory organ that can ingest multiple concurrent cognitive lanes, survive process and device restarts, retrieve from partial or paraphrased cues, explain why a candidate was activated, learn from lived outcomes, and preserve a strict distinction between candidate relevance and factual authority.

The earlier embedding-assisted architecture solved only one part of that problem: it gave natural-language inputs a learned semantic neighborhood. It did so by placing a dense neural embedding service directly inside the memory critical path. In the live code lineage, `boot.py` configured `OMEGA_EMBED_ENGINE=gemma_gguf`; `memory.local_embed` direct-dialed an OpenAI-style HTTP endpoint at `:52700` backed by a `llama.cpp` `llama-server`, bypassing the omega-runtime gateway; `memory.embed_backfill` maintained `pending_embeds.json`; the DeepSleep drain lived in `deepsleep_loop/loop.py`; and `memory.semantic_admission` embedded queries again for semantic recall. The legacy representation stored 768-float embeddings; the existing MRL-256 path is a read-side truncation during graph compilation, not a native 256-D encoder output.

Cut 4 has already changed this picture for non-identity writes. Native non-identity citizenship no longer requires the embedder and now publishes durable `ProjectionDelta`/dirty evidence after canonical persistence. Residual learned-embedding coupling remains in three places: the lobe-0 identity/self-vector path, query-time semantic admission, and structural graph `SEMANTIC_SIMILARITY` compilation. Therefore the write-time failure mode described below is historical for non-identity memory after Cut 4, but still relevant as the failure class this redesign is intended to eliminate completely.

This created three architectural pathologies.

1. **Serialization:** otherwise independent lanes converged on a shared neural embedding runtime during persistence and again during recall.
2. **Partial citizenship:** a canonical record could exist while its vector projection failed or remained pending, requiring repair/backfill state.
3. **Repeated semantic derivation:** semantic geometry was created once for the stored record and re-derived again for every query before vector comparison.

The objective of embedding retirement was therefore not ideological removal of learned models. It was removal of a learned model from persistent-memory ownership and from the synchronous mechanics that serialized the organ.

This paper asks whether the useful part of the learned model can be retained while eliminating the harmful part.

The proposed answer is yes: place a tiny frozen transformer at the boundary as a **semantic retina**, terminate it at a fixed INT8 latent interface, immediately translate that transient perception into binary geometry, and assign all persistent semantics, association, truth, and learning to deterministic Omega-native memory machinery.

---

## 2. Design Thesis

The central design law is:

> **A learned model may shape perception. It may not own persistent memory.**

This divides the system into two regimes.

### 2.1 Frozen prior semantics

The semantic retina answers only:

> What region of general human-language meaning does this input resemble?

Its parameters never change from Omega's lived experience. Its dense state is transient. It does not store memories, create graph edges, assign reward, or admit facts.

### 2.2 Plastic lived semantics

The memory organ answers:

> What has this concept meant, predicted, caused, or helped accomplish in Omega's actual experience?

That state is learned through structural evidence, exact execution traces, verified use, neuromodulation, reward-prediction error, DeepSleep consolidation, association potentiation/depression, decay, and forgetting.

The resulting system therefore combines a **frozen semantic prior** with a **growing experiential memory** rather than attempting to make one embedding space perform both jobs.

---

## 3. End-to-End Candidate Architecture

```text
MULTI-LANE NATURAL LANGUAGE / MEMORY RECORD
                    |
                    v
     BRAIN-RESIDENT MEMORY-OWNED SEMANTIC RETINA
 tokenizer -> frozen Quark INT8 ONNX transformer
                    |
                    v
              z_q : INT8[256]
                    |
====================|================================
        OMEGA BIT-EXACT MEMORY FRONTIER
====================|================================
                    v
       HQ-NP SIGN PROJECTION
 Gaussian reference / Rademacher hardware candidate
                    |
                    v
          h : {0,1}^16384
                    |
                    v
        DENSE SEMANTIC SURFACE
     separate Hamming/XNOR scorer
     separate geometry + generation
     semantic routing receipt only
                    |
                    +---------------------+
                                          |
CANONICAL / SYMBOLIC CUES                  |
          |                               |
          v                               |
 SPARSE STRUCTURAL DME                    |
 registered coordinates                   |
 typed graph / structural hops            |
          |                               |
          +---------------+---------------+
                          v
                   candidate fusion
                          |
                 RecallBasis / receipts
                          |
                 CANONICAL FILESYSTEM
                          |
                    RecallVerifier
                          |
                   cognition / action
                          |
                 exact use/eligibility
                          |
              outcome / RPE / modulators
                          |
                       DeepSleep
                          |
       +------------------+------------------+
       |                  |                  |
       v                  v                  v
engram accessibility  learned association  predictive/context
reconsolidation       potentiation/         topology
                      depression/decay
                          |
                          v
                learned sparse planes
```

The architecture is intentionally asymmetric: the retina is frozen; the organ below it is plastic. The 16K HQ-NP representation is **not** inserted into the current sparse DME matrix. It is a separate semantic surface with its own scorer, geometry, generation receipt, and evidence class. Sparse registered coordinates remain the structural DME substrate.

Functionally, this also preserves the useful intuition of hippocampal indexing: a compact associative index routes partial cues back toward richer stored content rather than becoming the content itself [1,2].


---

## 4. The Semantic Retina as a Hermetic Sensory Appliance

The semantic retina is deployed as a deliberately bounded **brain-resident memory appliance**, not as the legacy direct-dial embedding service and not as a general-purpose brain-side model loader. Under ADR-MO-018 the Omega3.0 memory system owns one resident session and invokes it in-process. Its model/tokenizer/pooling/output contract is immutable and generation-pinned. No HTTP, socket, IPC, omega-runtime worker, or alternate-encoder fallback exists for the retina.

### 4.1 Deployment form

Candidate build chain:

```text
frozen small semantic transformer (~20M parameters)
            |
            v
AMD Quark static quantization
            |
            v
INT8 ONNX export
            |
            v
truncate / expose canonical final QuantizeLinear tensor
            |
            v
semantic_retina_int8.onnx
```

No current live Omega path emits this artifact or a native 256-D Quark latent. This is a candidate build target; today's legacy path stores 768-float embeddings and only truncates to MRL-256 on the graph-compile read side.

The target output contract is:

```text
name:       z_q
shape:      [256]
dtype:      int8
granularity: per-tensor preferred
zero-point: 0 preferred
scale:      frozen generation parameter
```

The dense or dequantized terminal output is not required by the memory organ. The integer tensor itself is the interface.

### 4.2 Why the model becomes an appliance rather than a service

A brain-resident memory-owned seat replaces the prior direct-dial embedding-service contract:

```text
OLD
brain -> direct HTTP :52700 -> llama.cpp embedding server
     -> retries / health / backfill coupling -> vector response

NEW
brain/projector -> memory.semantic_retina resident session
               -> direct in-process encode/pool/normalize call
               -> INT8[256] + generation receipt
```

ADR-MO-018 explicitly overrules the former runtime-seat requirement for this component. The retina has no HTTP, socket, IPC, shared-memory transport, runtime-worker dependency, or runtime fallback. It is loaded once as a bounded memory-owned singleton with an explicit thread/resource budget. Failure or generation mismatch disables the semantic lane and emits a degradation/no-signal posture; it does not dial an alternate encoder. **Recall-time semantic perception remains a synchronous local dependency**, but transport/process failure is removed from that path.

### 4.3 Frozen generation manifest

A retina generation should pin the complete sensory contract:

```text
retina_generation_id
source_model_id
source_weights_sha256
semantic_retina_int8.onnx
onnx_sha256
onnx_opset
quark_version
quark_quantization_config
calibration_corpus_sha256
tokenizer artifact hashes
tokenizer normalization hash
output_tensor_name
output_dtype
output_shape
quantization_scale
quantization_zero_point
reference test vectors
reference z_q outputs
```

The goal is not universal cross-runtime reproducibility. Omega is a hardware/software co-designed system. The requirement is reproducibility and reference parity on the **certified Omega execution substrate**.

---

## 5. Hyperplane Quantized Neural-to-Binary Projection (HQ-NP)

Let the frozen retina produce a semantic latent

\[
\mathbf{z} \in \mathbb{R}^d,
\]

with target dimension \(d=256\). A sign-random-projection reference maps the latent into a binary hypervector of dimension \(D=16{,}384\):

\[
h_i = \mathbf{1}[(\Phi z)_i > 0],
\qquad \mathbf{h}\in\{0,1\}^D.
\]

For isotropic Gaussian or uniformly spherical random hyperplane normals, the probability that one hyperplane separates two vectors with angular distance \(\theta\) is [10]

\[
P[h_i(\mathbf{z}_1)\ne h_i(\mathbf{z}_2)] = \frac{\theta}{\pi},
\]

where

\[
\theta = \arccos\left(
\frac{\mathbf{z}_1\cdot\mathbf{z}_2}
{\|\mathbf{z}_1\|_2\|\mathbf{z}_2\|_2}
\right).
\]

Therefore the normalized Hamming distance over many independently sampled hyperplanes estimates the angular distance of the source semantic representation:

\[
\mathbb{E}\left[\frac{d_H(\mathbf{h}_1,\mathbf{h}_2)}{D}\right]
=\frac{\theta}{\pi}.
\]

This is the mathematical bridge that makes HQ-NP interesting: the neural model supplies semantic geometry once; the memory substrate receives a binary estimator of that geometry and can operate using Hamming/popcount-like primitives thereafter. With independent hyperplanes and separation probability \(p=\theta/\pi\), the normalized Hamming estimator has variance \(p(1-p)/D\). The fixed bank may have matrix rank at most the source dimension, but that does not invalidate the independent per-row collision law; it does constrain the family of realizable binary codes and should be characterized empirically.

### 5.1 Correct interpretation

HQ-NP does not create semantics from hashes. It preserves, approximately and statistically, neighborhood information already present in the semantic latent.

The dense latent may be destroyed immediately after projection. It does not need to become a stored vector.

### 5.2 Example correction

If cosine similarity is \(0.92\), then

\[
\theta=\arccos(0.92)\approx0.403\;\text{rad},
\]

so the Gaussian-reference expected normalized Hamming distance is approximately

\[
0.403/\pi\approx0.128,
\]

not \(0.08\). At \(D=16{,}384\), the expected differing-bit count is approximately \(2{,}100\). The exact distribution for a deployed semantic encoder must be measured rather than inferred from illustrative sentence pairs.


---

## 6. Integerized Rademacher HQ-NP as the Hardware Candidate

The Gaussian sign projection provides the clean angular-similarity reference. The native Omega implementation should separately evaluate an integerized Rademacher projection:

\[
\Phi_{ij}\in\{-1,+1\}.
\]

Given canonical INT8 latent \(\mathbf{z}_q\), each projection row computes

\[
s_i=\sum_{j=1}^{256}\Phi_{ij}z_{q,j},
\]

followed by a deterministic sign rule.

### 6.1 Hardware form

```text
INT8[256] latent
      |
      v
512 KiB fixed sign bitmap
(16,384 x 256 bits)
      |
      v
conditional add/subtract reductions
      |
      v
INT32[16,384] accumulator result
      |
      v
sign/tie rule
      |
      v
2,048-byte binary hypervector
```

The mask size is exactly

\[
16{,}384\times256 = 4{,}194{,}304\;\text{bits}
=524{,}288\;\text{bytes}=512\;\text{KiB}.
\]

No general multiplier is required for the projection: one mask state selects addition and the other subtraction.

### 6.2 Accumulator contract

The reference implementation should use INT32 accumulation. A signed INT16 accumulator is insufficient as a formal worst-case contract because a legal extreme can reach \(+32768\), while signed INT16 ends at \(+32767\).

### 6.3 Rademacher is a hypothesis, not a theorem substitution

Discrete Rademacher/discrete random projections have strong Johnson-Lindenstrauss lineage for linear metric preservation [9], but a Rademacher hyperplane distribution is not rotationally invariant in the same sense as Gaussian/spherical normals. Therefore this paper does **not** assume that the exact \(\theta/\pi\) sign-collision law transfers unchanged.

Instead, two arms must be measured:

```text
HQ-NP-G: isotropic Gaussian/spherical sign projection reference
HQ-NP-R: fixed {-1,+1} Rademacher hardware candidate
```

On the actual **Quark-quantized 256-D semantic-retina manifold**, not merely the float source manifold, measure

\[
\epsilon(c)=\left|
\frac{d_H(h_1,h_2)}{D}
-\frac{\arccos(c)}{\pi}
\right|.
\]

If HQ-NP-R tracks HQ-NP-G closely enough for retrieval quality while materially improving native execution simplicity, the Rademacher path becomes the stronger Omega candidate.

### 6.4 Tie rule

Integer reductions make exact zero sums possible. The final implementation should explicitly version the zero-sum behavior. Two valid research options are:

1. `GTE_ZERO`: \(s_i\ge0\to1\);
2. balanced deterministic tie bits \(t_i\) derived from the projection generation seed.

The second avoids a systematic one-bias when exact cancellations occur. Bit density and zero frequency must be measured before selecting the product rule. The experiment must also define zero-norm and near-zero-norm inputs explicitly: empty, stopword-only, or collapsed quantized latents must not fall through to an all-tie seed pattern that becomes a universal semantic neighbor. Such inputs should disable the semantic lane or receive a separately typed `semantic_retina_no_signal` receipt.

---

## 7. Structured Semantics: Role-Filler Binding

Sentence-level semantic proximity is insufficient for memory because relevance is not the same as proposition structure. Examples such as

```text
Alice managed Bob
Bob managed Alice
```

or

```text
patient has history of stroke
patient has no history of stroke
```

may remain close under a pooled semantic encoder even though they carry materially different claims.

A candidate repair is Vector Symbolic Architecture role-filler binding; sparse binary binding/thinning literature provides a relevant precedent [4]. For semantic atom codes \(h_x\) and fixed role hypervectors \(R_k\), a structured fact can be represented schematically as

\[
H_{fact}=B(R_{subj}\oplus h_{Alice},
           R_{rel}\oplus h_{managed},
           R_{obj}\oplus h_{Bob}),
\]

where \(\oplus\) is a binding operator and \(B\) is a sparsity-preserving or majority-style bundling operator.

The role vectors are deterministic projection artifacts. They do not create factual truth; they encode the **shape** of a candidate relation.

### 7.1 Required measurements

Do not assume role reversal becomes perfectly orthogonal. Measure:

- role reversal Hamming separation;
- negation separation;
- paraphrase invariance after factorization;
- bundling crosstalk as slot count grows;
- unbinding error;
- false candidate activation;
- DME query density and NPU cost.

The desired outcome is to preserve semantic invariance while increasing sensitivity to relational structure.


---

## 8. Auto-Associative Clean-Up Without Restoring Continuous Memory Authority

Bundling and partial cues inevitably introduce noise. A clean-up mechanism is therefore useful, but the clean-up layer must not silently reintroduce a continuous attention system as the authority over persistent memory.

Modern Hopfield networks demonstrate that associative clean-up and high-capacity retrieval are possible [11], but their common formulations use continuous states and attention/softmax-like updates. That makes them valuable conceptual references, not automatically the correct native Omega primitive.

The preferred research comparison is:

```text
A. current tiered OR-plane propagation
B. sparse Willshaw-style binary associative memory
C. bounded binary threshold / winner-take-all cleanup
D. continuous modern-Hopfield reference only as comparator
```

The native candidate should satisfy:

- bounded iteration count;
- exact deterministic reference semantics;
- contributor attribution or exact second-stage proof;
- no persistence of a dense state;
- no bypass of RecallVerifier;
- no unbounded recurrent spreading;
- hardware mapping compatible with binary/integer primitives.

A clean-up output remains a candidate address, never a claim.

---

## 9. DME Integration: Two Binary Surfaces, One Canonical Memory

Omega's current DME is a sparse coordinate-address substrate. HQ-NP produces a fundamentally different representation: a roughly balanced, dense 16K semantic code. Fable's live-code review showed that these representations must not be mixed into today's single `rows_bits` matrix: the current generic-bit document-frequency penalty would classify approximately half-density semantic bits as generic, current direct scoring is AND/popcount rather than Hamming/XNOR, and the geometry/plane registration path assumes one fixed sparse width.

The candidate architecture therefore requires **two distinct binary surfaces**:

```text
SPARSE STRUCTURAL SURFACE
  registry coordinates / typed graph / learned sparse planes
  current DME geometry lineage
  sparse overlap / bounded structural hop semantics

DENSE SEMANTIC SURFACE
  HQ-NP 16,384-bit codes
  independent geometry and storage
  Hamming/XNOR or mathematically equivalent normalized scorer
  independent DF/statistics policy
  explicit plane_id + composite generation_id
```

Both surfaces return canonical memory IDs. Neither surface owns canonical truth.

### 9.1 Write-time projection inherits the full Cut-4 transaction law

The abbreviated `canonical write -> ProjectionDelta -> projector` sketch is insufficient. The frozen producer contract is:

```text
durable WAL open (+ prior snapshot)
        |
        | not durable -> canonical mutation does not begin
        v
canonical write + encoding
        |
        | failure -> rollback / restore prior bytes
        v
canonical_finalized:<mode>
        |
        | not durable -> rollback / restore; NOT committed
        v
commit closes only on durable delta OR durable dirty evidence
        |
        v
owner projection_obligation entry
        |
        | must exist before canonical-mutating hooks
        | no durable recovery channel -> hooks are skipped
        v
atomic supersedes / entity-link hooks
        |
        | unknown mutation set != zero mutations
        v
final ProjectionDelta(s) OR durable rebuild-required marker
        |
        v
only now may the projection obligation close
```

The retina and HQ-NP projector are downstream consumers of this contract. They do not weaken or bypass it.

### 9.2 Projector-side obligations

Any async semantic projector must obey the following frozen rules:

1. **Latest delta per `memory_id` is authoritative work.** Earlier deltas may be superseded by a post-hook delta for the same canonical record.
2. **A delta is a pointer plus expected canonical state, not payload bytes.** The projector reads the canonical file at projection time. If the file hash differs from the delta hash because canonical truth is newer, the newer canonical file wins; the delta is not rewritten to manufacture consistency.
3. **`sequenced=False` is not source order.** A semantic row order or generation order must never silently infer causality from unsequenced `commit_seq` values.
4. **A durable projection-dirty marker requires full rebuild before any currentness claim.** Catching up on deltas is not enough to clear whole-profile dirty evidence.
5. **`filesystem_only` projection classes are not semantic citizens.** Trait-family records never pass through the retina merely because a projection work record exists.
6. **Open write-owner obligations and encoding journals are recovered by boot/recovery, not by the projector.** A projector must refuse to publish a coherent generation while unresolved producer recovery remains.
7. **Clean full rebuild remains the oracle.** An incremental semantic manifest that disagrees with clean rebuild is wrong.

These rules are what make the paper's claim of deterministic reprojection meaningful.

### 9.3 Recall-time perception is a bounded synchronous dependency

Recall still requires one live semantic-retina transformation when the semantic surface is requested:

```text
query text
   |
   v
brain-resident memory.semantic_retina seat
   |
   v
INT8[256] + retina generation receipt
   |
   v
HQ-NP
   |
   v
16K query code
   |
   v
separate Hamming/XNOR semantic scorer
   |
   v
semantic candidate IDs + exact execution receipts
   |
   +--------------------------+
                              |
sparse structural DME -------+--> candidate fusion
                              |
                              v
                        RecallVerifier
```

This is intentionally different from the old embedding service. Stored records are projected asynchronously and are not re-embedded at recall. However, semantic recall still depends synchronously on the retina being available. If the retina is unavailable, the semantic lane is absent and the system must degrade explicitly to lawful nonsemantic lanes.

### 9.4 Semantic routing proof: CUE != CLAIM

A 16K dense code has no per-bit canonical meaning. Therefore a semantic hit cannot reuse the existing `address_bit_set` proof used by sparse registered coordinates. The candidate design introduces a **SemanticExecutionReceipt** and a new research-only `semantic_code` basis class whose purpose is to prove routing, not truth.

At minimum the receipt binds:

```text
composite_generation_id
retina_generation_id
hqnp_generation_id
semantic_plane_id
scorer_version
query_text_hash
tokenizer_normalization_hash
query_code_hash
candidate_memory_id
candidate_code_hash
exact_hamming_distance
threshold_or_rank_policy_version
executed_attribution_id
status = semantic_hit | semantic_retina_no_signal | semantic_retina_unavailable
```

The receipt proves only:

> Under this exact frozen sensory/projection/scorer generation, this exact query code caused this canonical memory ID to enter the semantic candidate set with this exact distance/rank result.

It does **not** prove the candidate's proposition. A semantic-only hit is therefore **topical, never verified**. RecallVerifier may admit the underlying canonical record only through its ordinary canonical/provenance/currentness rules. The existing `semantic_recall` topical lane is the live precedent for this separation.

The semantic receipt must also be independent from long-lived trace heuristics. v0.3 chooses the conservative initial posture: **ordinary semantic routing uses the existing lane-flag/topicality path, not a new Cut-3-admissible `RecallBasis` kind**. The receipt records execution provenance; canonical authenticity/currentness verification remains independent. Audit sampling may re-run the pinned retina/HQ-NP pipeline to reproduce the query code and test receipt integrity. If a future design wants `semantic_code` to become a first-class verifier basis or plastic association contributor, it must add an explicit new basis kind and either re-run the retina in the verifier or provide another exact independently reproducible mechanism. Until then, semantic-only routing receives zero durable **association** credit.

This does not ban lawful **engram-level** learning. A semantically routed memory that independently becomes canonically verified, is demonstrably used by cognition (`U_a=1`), and has exact same-episode/generation evidence may still receive engram accessibility/reconsolidation credit under the ordinary StrengthIndex law. What may not be reinforced is the semantic Hamming relation itself or an unproven associative pathway.

### 9.5 Multi-lane batching

The runtime seat should batch independent lane requests rather than serialize them behind a per-request service loop:

```text
lane 0 query --+
lane 1 query --+
lane 2 query --+--> brain-resident retina batch --> INT8[B,256]
lane 3 query --+                                  |
                                                  v
                                          batched HQ-NP
                                                  |
                                                  v
                                      semantic-plane scorer
```

The exact throughput is an experimental result. The design target is a brain-resident, memory-owned, versioned transducer with one bounded in-process session—no model server, socket, runtime worker, or fallback encoder.

---

## 10. Neuromodulated Plasticity: How the Organ Actually Learns

A frozen sensory retina must not produce a frozen memory system. The central adaptive state lives below the bit-exact frontier in Omega's engrams, associations, predictive topology, accessibility tiers, and consolidation machinery.

The relevant biological analogy is not ordinary two-factor Hebbian learning alone. Three-factor learning rules distinguish local activation, eligibility, and later modulatory signals [6,7]:

1. local coactivation or pathway participation;
2. a temporary eligibility trace;
3. a later modulatory signal such as reward, punishment, surprise, or novelty that converts eligibility into durable plasticity.

This maps to Omega's **target architecture**, but the live implementation does not yet satisfy the full target.

### 10.1 Live-state reconciliation: the current credit loop is not yet the target organ

Fable's live-code audit found an existing ADR-MO-003 violation that this paper must not hide:

- `reinforce_recalled_memories()` currently calls `StrengthIndex.reinforce()` during the cognition cascade at three live call sites;
- `OMEGA_DOPAMINE_REINFORCE=1` is enabled in the boot profile;
- DeepSleep currently owns decay/demotion/reconsolidation `touch()` work, but not all positive/negative durable reinforcement;
- no durable association credit receipt exists yet;
- `AssociationStrengthIndex` is target state, not live code;
- `U_a`, meaning proof that a memory/path was actually used by cognition, does not exist today;
- current missing relevance defaults to full weight `1.0`, which is the opposite of the fail-closed use gate proposed here;
- the live memory-credit path is retrieval/relevance keyed, not response-use keyed.

Therefore the following sections describe the **required target topology and Program-II preconditions**, not the current implementation.

### 10.2 Four distinct states

The target organ must explicitly preserve four different concepts:

```text
ACTIVATION
  a path or association participated in executed retrieval

ELIGIBILITY
  same-generation execution evidence plus verifier/use evidence
  makes that exact path eligible for later outcome credit

CREDIT
  a later episode outcome produces positive, negative, or predicted RPE
  under the episode's preserved neuromodulatory state

DURABLE PLASTICITY
  the owning DeepSleep applies bounded potentiation/depression/decay
  to adaptive sidecars and republishes derived learned planes
```

Therefore:

```text
activation != eligibility
eligibility != credit
credit != immediate mutation
retrieval rank != use
semantic similarity != reward
```

### 10.3 Exact eligibility and the missing use gate

For learned association \(a\), define an episode-local target eligibility value schematically as

\[
e_a = A_a \cdot U_a \cdot V_a \cdot P_a,
\]

where:

- \(A_a\) is execution-proven contribution from the actual winning sparse/learned pathway;
- \(U_a\in\{0,1\}\) indicates that cognition/action actually used the candidate/path;
- \(V_a\in\{0,1\}\) indicates verifier-admitted canonical evidence;
- \(P_a\in\{0,1\}\) indicates exact episode and composite projection-generation attribution.

The live system does not yet provide \(U_a\). Program II is therefore blocked until a use receipt is defined from the actual cognition/action output rather than inferred from retrieval rank. Missing use/relevance must fail closed to zero credit, not default to full credit.

If the contributing edge/path cannot be mechanically proven from the execution that produced the candidate, \(P_a=0\) and durable association credit is zero. Topological plausibility is explanation, not learning eligibility. Semantic-plane Hamming distance alone is not an eligible \(A_a\) because it proves perceptual neighborhood, not an experiential association pathway.

### 10.4 Neuromodulatory state

Let the closed episode expose a modulatory vector

\[
\mathbf{m}=(\delta_{RPE},\; n,\; s,\; p,\; \sigma,\ldots),
\]

where candidate terms may include:

- \(\delta_{RPE}\): reward-prediction error / dopamine-like outcome signal;
- \(n\): novelty or surprise;
- \(s\): stress/arousal state;
- \(p\): global plasticity gate;
- \(\sigma\): stability/serotonin-like state or other Symphony modulators.

The live memory-credit path currently uses only a subset of this conceptual vector; novelty/noradrenaline and other Symphony signals are not yet wired as one memory credit rule. The schema should preserve signals separately and make the combine policy versioned and measurable rather than inferred from biological metaphor.

A generic target durable update can be written as

\[
\Delta w_a = \eta \; e_a \; g(\mathbf{m}),
\]

with bounded \(w_a\) and a versioned policy \(g\). Positive RPE may potentiate eligible pathways; negative RPE may depress them; predicted outcomes may produce no new choice credit.

### 10.5 DeepSleep must own durable mutation

The target repair is:

```text
runtime recall / cognition
  -> DMEActivationTrace
  -> RecallBasis / SemanticExecutionReceipt
  -> exact contributor receipt
  -> verifier result
  -> actual-use receipt U_a
  -> episode-local neuromod/RPE receipt
  -> NO durable adaptive mutation

owning DeepSleep
  -> validates same episode + composite generation
  -> applies StrengthIndex reconsolidation/accessibility
  -> applies AssociationStrengthIndex potentiation/depression
  -> decay / dormancy / retirement / homeostasis
  -> republishes derived learned planes
```

This is the architectural law already recorded in ADR-MO-003. The current cascade-time reinforcement is a violation-to-fix, not precedent for weakening the law.

### 10.6 Reward-loop hazard and fail-closed credit

The present retrieval-keyed reinforcement path can form a positive feedback loop: a highly ranked memory is recalled, reinforcement increases its `StrengthIndex`, and the increased strength can make it rank even higher later. The existing dead band/clamp may bound magnitude but has not proven causal correctness.

The developmental organ must therefore measure and prevent:

- retrieval-rank self-reinforcement;
- full credit from missing relevance/use evidence;
- cross-lane episode leakage;
- credit to inferred epochs or mixed generations;
- credit dilution from larger semantic candidate fanout;
- observation/co-occurrence being mistaken for outcome reward.

Program II must not begin until the cascade-time reinforcement path is relocated behind a DeepSleep-owned credit receipt and the explicit use gate exists.

---

## 11. Two Semantic Systems: Prior Meaning and Lived Meaning

The proposed organ intentionally contains two different kinds of semantics, consistent with the broader complementary-learning-systems argument for a stable integrative system plus a rapid episodic system [8].

### 11.1 Frozen prior semantic geometry

The retina/HQ-NP path provides general linguistic invariance:

```text
heart attack ~ myocardial infarction
car ~ automobile
purchase ~ buy
```

This is inherited prior knowledge from the frozen model. It does not become more personally important merely because it is semantically close. The semantic-plane Hamming score is therefore a routing/ranking signal only; it must not be reused as the neuromodulated credit weight for experiential learning.

### 11.2 Experiential semantic topology

Omega's lived associations may learn relationships that the frozen model does not know, does not emphasize, or represents incorrectly for this system:

```text
IRON -> XDNA2 -> DME -> memory scorer
particular tool -> particular failure mode
specific person -> specific project context
specific observation -> successful action sequence
```

These relationships are not encoded by retraining the retina. They are learned by changing **connectivity and accessibility below the binary frontier**.

This preserves stable sensory perception while allowing the memory organ to become increasingly specific to its own history.

### 11.3 Observation is not reward

Co-occurrence, repeated documents, or semantic closeness may support structural evidence or candidate-association creation. They do not automatically produce dopamine-like experiential reinforcement.

Durable experiential strength requires outcome-gated evidence. This prevents frequent but useless correlations from becoming dominant shortcuts.

---

## 12. Growth, Prediction, and Context

A mature organ should be able to learn more than pairwise similarity.

### 12.1 Learned association classes

Candidate adaptive relations include:

```text
COACTIVATED_WITH
USED_TOGETHER
SAME_GOAL
TEMPORALLY_LINKED
PREDICTS
REPEATED_CONTEXT
```

They are retrieval/accessibility state, not new canonical facts.

### 12.2 Predictive memory

Repeated verified sequences can produce directed predictive accessibility:

\[
A \rightarrow B \rightarrow C
\]

where the learned state captures not merely that A and B are related, but that B often follows A under a relevant context and useful outcome. This is conceptually related to predictive-map/successor-representation work [5], but Omega can implement the useful principle without adopting a continuous RL representation as memory truth.

### 12.3 Projection-only context states

Polysemy and aliased observations may eventually justify context-specific projection states:

```text
IRON @ metallurgy
IRON @ Omega/XDNA toolchain
```

Such states must remain derivative and rebuildable. Context-specific hidden-state graph models provide one relevant research analogue [12]. Canonical identities do not split merely because projection context does.

### 12.4 Homeostasis and competition

Unbounded positive reinforcement would eventually create hubs whose outgoing associations all saturate. Consolidation research also links replay, sparsification, pruning, and homeostatic scaling [13]. The adaptive organ therefore needs a measured homeostatic policy, potentially including:

- fanout caps;
- normalization or finite association budgets;
- tier competition;
- negative-RPE depression;
- inactivity decay;
- dormancy and retirement;
- replay-driven pruning.

The exact rule is an experimental question, but some anti-saturation mechanism is required if the organ is to keep growing without losing selectivity.


---

## 13. Forgetting as Accessibility Plasticity

Forgetting should not mean deleting canonical truth merely because a pathway became weak.

The organ separates:

```text
canonical record existence      -> filesystem / supersession law
structural relation existence   -> compiler-proven topology
engram accessibility            -> StrengthIndex
learned association usefulness  -> AssociationStrengthIndex
active plane membership         -> derived projection state
```

A learned association may move through

```text
active -> weak -> dormant -> retired
```

without deleting the records it once connected. Negative RPE may accelerate depression; inactivity may decay experiential accessibility; successful reuse may restore it. Structural relations remain reconstructible from canonical evidence regardless of experiential strength.

This produces an important asymmetry:

> **Truth is durable; accessibility is plastic.**

That asymmetry is required for an organ that can both learn and unlearn retrieval habits without rewriting history.

---

## 14. Epistemic Separation of Authority

The architecture deliberately prevents one representation from serving incompatible epistemic roles.

| Layer | Mechanism | Question it may answer | What it may not decide |
|---|---|---|---|
| Semantic retina | Frozen quantized transformer resident in Omega3.0 memory | What does this language resemble? | Whether a memory is true, useful, or deserving of reward |
| HQ-NP | Fixed sign projection | What binary neighborhood represents that perception? | Whether a candidate is evidence |
| Dense semantic surface | Same-generation 16K codes + Hamming/XNOR scorer | Which canonical IDs are perceptually near this cue? | Whether proximity is factual support or learning credit |
| SemanticExecutionReceipt | Exact scorer/generation/hash receipt | Why did this semantic candidate enter the set? | Whether its proposition is true |
| Role/VSA layer | Deterministic binding | What structural roles are expressed by the cue? | Whether the proposition is canonical truth |
| Sparse structural DME | Registered coordinates + typed hops | What structural addresses/pathways were executed? | Whether a candidate is factually admissible |
| Structural graph | Typed rebuildable topology | What explicit relations are supported by canonical state? | Whether a path was useful in this episode |
| Association state | Neuromodulated adaptive connectivity | What proven pathways have become useful or harmful? | Whether a relation is a canonical fact |
| Canonical filesystem | Durable source records | What was actually stored, with provenance/currentness? | Which memory should be retrieved first |
| RecallVerifier | Fail-closed admission | What canonical evidence may cognition use now? | How the retina should encode language |

This separation is the core safety and auditability property of the design.

A high Hamming score cannot become truth. A semantic routing receipt proves execution, not a proposition. A highly rewarded association cannot fabricate evidence. A structural graph edge cannot prove that it caused a successful recall. A frozen neural prior cannot rewrite lived memory. Semantic-only routing remains topical until canonical verification, and semantic-only routing receives zero durable association credit until a same-generation exact use/contributor proof exists.

---

## 15. Semantic Retina Generations and Non-Destructive Upgrades

The canonical filesystem allows sensory representations to evolve without erasing history.

### 15.1 Generation model

```text
CANONICAL MEMORY
      |
      +--> Retina Gen 1 -> HQ-NP Gen 1 -> Semantic Plane Gen A
      |                                      |
      |                                      +--> Projection Manifest Gen A
      |
      +--> Retina Gen 2 -> HQ-NP Gen 2 -> Semantic Plane Gen B
                                             |
                                             +--> Projection Manifest Gen B
```

A retina upgrade does not mutate the canonical payload merely because the new transducer perceives it differently.

### 15.2 Reprojection

If Gen 2 changes the tokenizer, encoder, quantizer, projection bank, semantic scorer, or role-binding schema, existing canonical records are re-run through the entire Gen-2 projection pipeline in the background. This is a **projection migration**, not a memory rewrite.

Stable objects remain stable:

```text
memory IDs
canonical paths/hashes
provenance
supersession history
structural source evidence
association identities based on canonical endpoints
outcome-credit history
```

Derived objects may change:

```text
semantic binary codes
semantic-plane rows / codebook entries
projection-only context states
role/filler projection artifacts
learned-plane materialization under the new generation
```

Hypervector bits never become registry coordinate keys, canonical IDs, or `memory_id` seat tokens.

### 15.3 One composite generation identity

Independent generation pins checked in independent places invite mixed-generation execution. Cut 13 should therefore define one composite generation identifier, for example:

```text
generation_id = sha256(canonical_json({
  retina_generation,
  hqnp_generation,        # mask + tie rule + zero-signal rule
  projection_generation,  # canonical/projected watermarks and plane hashes
  dme_schema_version,
  scorer_versions
}))
```

The projector refuses to publish a semantic plane whose sensory/projection components do not match the target manifest. `DmeExecutionBinding`/semantic execution receipts carry this single composite ID. The composite acts as an **atomic compatibility epoch**, not a demand to physically rebuild every unchanged surface. If only the semantic surface changes, a new composite manifest may reference the unchanged sparse-surface hash/generation and the new semantic-surface hash/generation; the manifest switch is atomic. Readers operate against one compatibility epoch and never mix components from different epochs.

This composite identity supersedes the narrower current `projection_generation` notion for multi-surface execution once validated. It extends the existing no-mixed-generation requirement rather than replacing it.

---

## 16. Failure Semantics and Removal of Backfill Debt

The proposed design changes what a semantic-transducer failure means, but the paper must distinguish historical legacy behavior from the Cut-4 non-identity state.

### 16.1 Legacy failure mode and current residual coupling

Historically:

```text
canonical write
   |
embedding service fails
   |
pending_embeds / backfill / retry state
   |
record exists while vector projection is incomplete
```

For non-identity writes, Cut 4 has already removed this learned-model citizenship requirement and replaced it with canonical truth plus derivative projection work. The remaining legacy learned-embedding coupling is:

```text
identity / lobe-0 self-vector path
query-time semantic_admission
SEMANTIC_SIMILARITY graph compilation
```

Until Cut 12 removes or redesigns the identity/self-vector dependency, any live retina seat would coexist with the legacy embedder for identity. Therefore Program-I offline semantic experiments can begin before Cut 12, but a clean H4 runtime comparison and Program-II live-organ claim are gated on Cut 12 or an explicit identity-on-retina decision.

### 16.2 Proposed projector failure mode

```text
canonical transaction reaches durable finalization
   |
durable ProjectionDelta(s) / dirty evidence / obligations exist
   |
retina or projector fails
   |
canonical memory remains valid
old coherent projection generation remains readable
   |
projector restarts only after producer recovery is clean
   |
latest canonical bytes/hash are projected under one composite generation
```

There is no separate semantic-citizenship state whose repair requires a special embedding queue. Projection lag is ordinary derivative-state lag. A dirty marker forces full rebuild before a currentness claim; deltas are work pointers, not truth payloads; clean full rebuild remains the oracle.

### 16.3 Recall-time failure requires a machine-readable lane receipt

If the retina is unavailable at recall time, the system degrades explicitly to lawful sparse/lexical/scoped/temporal lanes. This must be represented as a basis/lane receipt such as:

```text
lane = semantic
status = semantic_retina_unavailable
composite_generation_id = <expected generation>
semantic_candidates = 0
fallback_lanes = [...]
```

Telemetry alone is insufficient. The absence of semantic perception must remain mechanically visible so `unverified admissions = 0` and cross-lane auditability remain provable.

The failure must not alter canonical memory or fabricate an all-tie/zero-latent semantic cue.

---

## 17. Omega-Specific Hardware/Software Co-Design

This architecture is not intended to be a generic memory library optimized for the least-common-denominator machine. Omega's certified native target includes XDNA 2, IRON/MLIR-AIE tooling, custom binary DME kernels, and a resident local model/runtime stack.

The portability contract is therefore:

> **Reference semantics define correctness; the native Omega substrate defines the performance architecture.**

### 17.1 Reference implementation

A CPU/reference path should define:

- tokenizer semantics;
- runtime-owned ONNX retina input/output contract;
- canonical INT8 latent and zero-signal rule;
- Gaussian-reference HQ-NP;
- Rademacher HQ-NP;
- tie rules;
- semantic Hamming/XNOR scoring;
- sparse structural DME scoring;
- VSA binding/bundling where tested;
- SemanticExecutionReceipt semantics;
- exact contributor/use proof;
- neuromodulated update equations;
- composite generation hashes.

### 17.2 Native implementation

The native path may aggressively exploit:

- an omega-runtime-owned resident quantized ONNX session;
- preallocated/batched tensor buffers;
- narrow local IPC/shared-memory transport where useful;
- fixed local projection masks;
- integer reductions;
- XDNA2 AIE tile parallelism where actual placement is proven;
- IRON data movement and kernel fusion;
- packed sparse DME planes;
- a separate dense semantic scorer;
- tightly coupled candidate scoring where parity permits.

The 512 KiB Rademacher mask is a logical artifact size, not a promise that one physical SRAM block will hold it. Its exact tiling across the XDNA2 memory hierarchy is a hardware-design problem to solve after the mathematical contract is frozen.

A Quark INT8 ONNX transformer is **not assumed to run on XDNA2 merely because it is INT8**. Existing proof lineage includes CPU fallback for some ONNX/QDQ paths. Transformer placement, intermediate-tensor support, memory residency, and contention with the language model/DME must be measured on the certified machine.

If the separate semantic surface is validated, it also requires an explicit extension of the packed-runtime specification rather than being silently folded into frozen sparse-DME docs. The natural specification home is the later multi-plane runtime work (Cut 15 lineage): `register_plane`/plane identifiers, `_artifact_fingerprint`, `DME_SCHEMA_VERSION`, `HopPlanes`, `_BITS_CACHE` budgeting, `DmeExecutionBinding`, and any future `RECALL_BASIS_KINDS` extension are shared seams that must be versioned deliberately.

### 17.3 Native exactness rule

Hardware is never allowed to redefine semantics to gain throughput. Where the contract is exact, native output must equal the reference output. Where an approximation is explicitly under research—for example Rademacher versus Gaussian angular preservation—the approximation is measured at the **algorithm-choice level**, then its selected implementation becomes exact against its own reference.

### 17.4 Program 0 — Representation-neutral projector and generation harness

Before live semantic-plane experiments, Omega needs the representation-neutral machinery that does not yet exist: a bounded outbox consumer plus Cut-13 composite manifest and crash tests. Program 0 does **not** choose HQ-NP versus sparse expansion; it establishes the lawful place where any winning projection will live.

Minimum Program-0 contract:

```text
consume latest ProjectionDelta work by memory_id
respect dirty/full-rebuild semantics
refuse unresolved producer obligations
explicitly classify sequenced=False work; never infer source order from its commit_seq
batch canonical reads + hashes
publish generation atomically
never truncate canonical files
use atomic replacement for manifests/derived artifacts
support plane_id / width / scorer metadata without assuming one geometry
record canonical/projected watermarks + composite generation_id
```

Program-0 **planning/TDD may begin now**, but under the frozen build sequence its implementation follows Cut 5A green because Cut 13 is sequenced after Cut 5A and the outbox consumer is Cut-14A lineage. Re-sequencing implementation requires an operator ruling.

Crash matrix:

- death after reading a delta but before writing derived state;
- death during plane publication;
- torn/interrupted manifest publication;
- generation switch mid-batch;
- dirty marker appearing mid-generation;
- projector restart with superseded deltas;
- canonical file newer than delta hash;
- missing canonical file/tombstone;
- unresolved write-owner obligation at startup.

Program 0 is the harness for later live experiments and preserves the byte-level discipline learned in Cut 4.

---

## 18. Research Program I — Static Semantic Bridge

The first research program asks whether a frozen retina plus binary projection gives Omega useful semantic invariance without restoring dense-vector memory authority. Program I begins offline; runtime-seat throughput work follows only after Program 0 and the identity/Cut-12 gate are resolved.

### 18.1 Minimum ablation arms

```text
A      current lawful symbolic / sparse DME cues

A'-full  frozen pre-Cut-5A legacy semantic_admission control
         top-k cosine seeds + one closure hop over compiled SEMANTIC_SIMILARITY edges
         requires a legacy-embedded text_768 corpus

A'-seed  post-Cut-5A semantic_admission control
         top-k cosine seeds only; SEMANTIC_SIMILARITY closure removed
         requires a legacy-embedded text_768 corpus

B      symbolic cues
       + deterministic sparse/Kenyon-style expansion [3]
       # research comparator, not required to justify HQ-NP

C-G    frozen semantic retina
       + float and Quark-INT8 latent measurements
       + Gaussian/spherical sign-hyperplane reference
       + separate semantic Hamming scorer

C-R    frozen semantic retina
       + Quark-INT8 latent
       + Rademacher sign projection
       + separate semantic Hamming/XNOR scorer

D-R    C-R
       + explicit role/filler VSA factorization
       + candidate binary cleanup mechanism
       # run only after the static bridge/coexistence probes pass
```

No arm may bypass RecallVerifier. A' must be run on a deliberately frozen legacy-embedded corpus because post-Cut-4 native non-identity writes carry no `text_768` vector and would otherwise make the control silently return `no_record_vectors`. Pre-register whether a comparison uses A'-full or A'-seed; do not mix them after Cut 5A changes the live closure semantics. Semantic routing confers topicality only; canonical verification remains independent and may still verify the underlying record.

### 18.2 Static geometry experiment

Before recall benchmarking, test at least two candidate small encoders and measure the geometry at every boundary:

```text
float latent
   vs
Quark INT8 latent
   vs
HQ-NP-G code
   vs
HQ-NP-R code
```

Required measurements include:

- kNN@10 neighborhood Jaccard between float and INT8 latents;
- Spearman correlation over pairwise cosine ordering;
- Gaussian-reference normalized Hamming distance;
- Rademacher normalized Hamming distance;
- Rademacher angular-estimator error on the **quantized** manifold;
- D in `{4096, 16384}`;
- bit density and exact-zero rate;
- `GTE_ZERO` versus balanced deterministic tie bits;
- zero/near-zero latent incidence and no-signal behavior.

### 18.3 Recall corpus classes

Each underlying fact should be probed with controlled variants:

- exact lexical restatement;
- morphological variation;
- synonyms;
- zero-token-overlap paraphrases;
- domain terminology versus lay terminology;
- role reversal;
- negation;
- temporal update/current-versus-historical wording;
- polysemy/context shift;
- underspecification;
- multi-hop question;
- noisy distractors;
- adversarial near-neighbors.

### 18.4 Recall and systems metrics

Measure at minimum:

```text
candidate recall@K
verified recall@K
false candidate activation
unverified admissions       # hard target 0
role-reversal separation
negation separation
paraphrase invariance
semantic candidate fanout
semantic code density
sparse DME query density
single-query retina latency
single-query HQ-NP latency
semantic scorer latency
batch throughput
p50/p95 under multi-lane load
resident runtime footprint
CPU/native parity
projector I/O per delta / per batch
canonical read+hash cost
```

The projector I/O measurements matter because canonical reads/hashes from the USB-resident tree may dominate semantic inference cost even if the retina/HQ-NP kernel is fast.

### 18.5 Coexistence probe

Explicitly compare:

```text
separate-plane Hamming/XNOR ranking
        vs
naive same-matrix AND/popcount ranking
```

with the existing generic-bit DF penalty enabled and disabled. The purpose is to quantify, not merely assert, why a roughly 50%-dense semantic surface cannot share today's sparse scoring semantics.

### 18.6 Runtime-seat throughput experiment

Only after Program 0 and Cut 12 (or an explicit identity-on-retina decision), compare:

```text
legacy :52700 llama.cpp embedding service
        vs
omega-runtime resident semantic retina
```

under batch sizes `B in {1,4,8}` and four concurrent cognitive lanes. Measure p50/p95, throughput, memory residency, scheduler contention, failure/degradation behavior, and whether the semantic seat contends with the language model or DME hardware. H4 is specifically about removal of **write-time/backfill service coupling and multi-lane service serialization**; recall still contains one bounded semantic-perception call.

### 18.7 Required conclusion

The experiments must be able to falsify the architecture. Examples of disconfirming results include:

```text
INT8 destroys source semantic neighborhood ordering
C-R materially diverges from C-G on the deployed latent manifold
C-R improves recall but creates unacceptable false semantic fanout
separate semantic scorer offers no useful gain over live A' cosine control
semantic execution receipts cannot be audited reproducibly
runtime seat still becomes a multi-lane serialization point
D-R fixes role reversal but destroys paraphrase invariance
```

A failure is a design result, not an inconvenience to hide. Only after the static bridge, recall arms, and separate-plane coexistence probe pass should D-R/VSA/cleanup become implementation candidates.

---

## 19. Research Program II — Developmental Neuromodulated Learning

Static semantic quality is insufficient. The organ must demonstrate that it becomes more useful through experience while the retina remains unchanged. Program II is **not permitted to start on the current cascade-time reinforcement path**.

### 19.1 Program-II preconditions

Before developmental learning is measured, the live organ must have:

```text
Cut 6: AssociationStrengthIndex durable sidecar
Cut 7: cascade-time StrengthIndex.reinforce() removed/disabled as durable mutation
Cut 7: explicit episode-local credit receipt + U_a actual-use receipt
Cut 7: missing use/relevance -> zero credit
Cut 7: same-episode + composite-generation validation
Cut 7: inferred multi-lane epoch resolution -> P_a = 0
Cut 8: DeepSleep-owned consolidation / positive and negative durable updates
Cut 9: derived learned-plane publication
semantic-only routing excluded from association credit unless exact second-stage proof exists
```

The open C-1 / RQ-MO-014 operator ruling on cascade-time reinforcement versus ADR-MO-003 remains owed before Cut 7 implementation.

Synthetic tests must first measure positive/negative credit precision and cross-lane leakage. Program II is invalid if run on top of retrieval-keyed self-reinforcement.

### 19.2 Developmental phases

Use a controlled sequence such as:

```text
DAY ZERO / OBSERVE
  frozen semantic prior only
  structural graph on
  traces on
  zero experiential association credit unless all gates are proven

LEARNING
  verified use/outcome history sufficient
  association creation enabled conservatively
  positive/negative RPE updates active in DeepSleep

MATURE
  stable learned pathways
  predictive associations
  decay/dormancy/homeostasis
  reversal and unlearning tests
```

The exact exit criterion from observation mode is measured rather than record-count-only.

### 19.3 Learning tasks

Construct tasks where the frozen retina alone is insufficient, for example:

- arbitrary project/tool relationships introduced after model training;
- new local terminology;
- repeated tool-action-outcome sequences;
- context-dependent meanings of common words;
- user-specific associations;
- changing success contingencies;
- initially useful pathways that later become harmful;
- distractor co-occurrence without successful outcome.

### 19.4 Learning metrics

Measure:

```text
verified task success before/after experience
learned-hop contribution to successful recall
exact attribution coverage
actual-use receipt coverage
positive-credit precision
negative-credit precision
association creation rate
false learned-association rate
strength-tier churn
fanout / hub growth
association entropy or concentration
cycles to learn
cycles to reverse/unlearn
path monopoly
recovery after negative RPE
engram versus association credit separation
cross-lane credit leakage
mixed-generation eligibility
retrieval-rank self-reinforcement
```

### 19.5 Hard safety invariants

Across all developmental experiments:

```text
silent misroutes = 0 target
unverified admissions = 0
credit to semantic-only similarity = 0 unless exact eligible pathway proof exists
credit to heuristic/stale path = 0
credit to wrong episode = 0
credit to inferred/mixed generation = 0
missing use evidence -> credit 0
canonical truth rewritten by reward = 0
observation-only co-occurrence translated directly into dopamine credit = 0
lost canonical writes = 0
mixed projection generations accepted = 0
```

A learning system that improves average retrieval while violating these invariants is not acceptable.

---

## 20. Falsifiable Hypotheses

This proposal should advance only if its central claims survive measurement.

### H1 — Frozen semantic perception improves zero-overlap recall

A small frozen retina feeding HQ-NP should materially improve semantic candidate recall on synonym and zero-lexical-overlap queries compared with purely lexical/symbolic cues, without increasing unverified admission.

### H2 — Binary geometry preserves enough of the semantic manifold

HQ-NP-G should show the expected angular/Hamming relationship, and HQ-NP-R should approximate the useful neighborhood ordering closely enough on the actual 256-D latent distribution to retain retrieval quality.

### H3 — Structural binding repairs semantic false-neighbor failures

Role/filler factorization should increase separation for role reversal, negation, and other structurally distinct propositions without destroying useful paraphrase invariance.

### H4 — Runtime-owned retina removes the former write/backfill serialization pathology

An omega-runtime-owned quantized ONNX retina with batched local calls should scale better across concurrent lanes than the legacy direct-dial `:52700` embedding service and eliminate embedding-specific write-time backfill/health semantics from memory correctness. H4 does **not** claim that semantic recall becomes model-free: one bounded synchronous retina pass remains when the semantic lane is requested, with explicit no-signal/unavailable receipts and lawful fallback. The clean runtime comparison is gated on Program 0 plus Cut 12 (or an explicit identity-on-retina decision).

### H5 — Frozen prior plus plastic connectivity outperforms either alone

The static retina should provide useful day-zero language generalization, while neuromodulated association learning should improve system-specific tasks that cannot be solved from the pretrained semantic prior alone. This is a research hypothesis, not a lever that automatically overrides the frozen build-first architecture if a benchmark score is attractive.

### H6 — Negative credit and homeostasis preserve long-term selectivity

The learned organ should be capable of reversing once-useful pathways, preventing high-frequency hubs from monopolizing recall, and retiring low-value associations without deleting canonical truth.

### H7 — Hardware specialization does not alter semantics

Native XDNA2/IRON implementations should meet the reference contract for all selected deterministic primitives and should expose any deliberately approximate algorithmic choice as a versioned, measured component.

---

## 21. Risks and Open Questions

### 21.1 Retina output geometry

A generic 256-D output is not automatically optimal for angular sign projection. Pooling strategy, normalization, quantization scale, and the source model's training objective may strongly affect neighborhood quality.

### 21.2 Quantization distortion

The retina must be evaluated before and after Quark quantization. If the INT8 frontier materially changes semantic neighborhoods, either the calibration/configuration or the chosen model is wrong for this role.

### 21.3 Rademacher versus Gaussian projection

Rademacher hardware simplicity is compelling, but its sign-collision behavior must be characterized empirically rather than inferred by substituting one random-projection theorem for another.

### 21.4 Architectural requirement: dense semantic surface is separate from sparse DME geometry

Fable's live-code review resolves this from an open risk into a design requirement for the experiment architecture. A roughly 50%-dense 16K HQ-NP code must **not** be inserted into today's sparse `rows_bits` matrix or scored by today's generic-bit/AND-popcount semantics. The research implementation requires a distinct semantic surface with its own width, plane ID, composite generation ID, Hamming/XNOR-equivalent scorer, statistics/DF policy, geometry validator, execution receipt, cache/residency budget, and evidence class. Only after this separate surface is measured may a later simplification be considered.

### 21.5 Coordinate identity

Semantic hypervector bits must not become canonical memory IDs, registry keys, or DME seat tokens. Locator identity, canonical record identity, registry coordinates, and semantic perceptual signatures remain distinct namespaces. The Cut-2 lessons around `memory_id` seat tokens and tokenizer punctuation make this especially important: the registry must not be widened to admit individual HQ-NP bits simply to make a dense code look like sparse coordinates.

### 21.6 Attribution through cleanup

Any auto-associative cleanup or bundled VSA layer that erases contributor identity may reduce the coverage of exact plasticity credit. Winner-only proof or contributor-preserving execution may be required.

### 21.7 Retina generations

Reprojection under a new sensory generation can change candidate neighborhoods. Historical adaptive credit must remain attached to stable canonical association identities, not to obsolete bit positions.

### 21.8 Plasticity stability

Neuromodulated learning can still create runaway pathways, reward hacking, or false correlation if eligibility, outcome relevance, or homeostasis are poorly designed. The verifier protects factual admission but does not by itself guarantee healthy learning dynamics.

### 21.9 Model size is not a law

“20M parameters” is a useful target class, not an architecture constant. The smallest model that supplies adequate semantic invariance at acceptable native throughput should win.

### 21.10 Semantic basis and exactness boundary

The central unresolved epistemic question is not whether HQ-NP can retrieve semantic neighbors, but whether a semantic hit can be made lawfully auditable without turning similarity into factual authority. The current proposal answers conservatively:

- `SemanticExecutionReceipt` proves same-generation scorer execution and deterministic routing;
- semantic-only candidates are topical, not verified;
- semantic Hamming score is excluded from experiential credit;
- any future semantic-path plasticity requires exact second-stage reproduction or another mechanically proven contributor mechanism;
- if this cannot be made affordable and reliable, the semantic surface may remain a diagnostic/topical recall lane rather than a plastic learned pathway.

This is the strongest falsifiable failure mode of the architecture.

---

## 22. Relationship to the Frozen Omega Memory Architecture

This paper does not propose abandoning the architectural work already frozen through Cut 4. Most of that work becomes more valuable under this design.

### 22.1 Preserved laws and Cut-4 producer guarantees

```text
canonical filesystem sovereignty
ProjectionDelta / asynchronous projection law
projection lag legal, silent loss illegal
full rebuild as correctness oracle
stable canonical identities
RecallBasis / RecallVerifier
CUE != CLAIM
DMEActivationTrace
exact episode + generation attribution requirement
ADR-MO-003: DeepSleep owns durable adaptive mutation   # ratified target; live C-1 violation to fix
separate StrengthIndex and AssociationStrengthIndex    # target separation; association sidecar not live yet
structural graph rebuildability
traits outside semantic DME citizenship
hardware semantics defined by a reference implementation
write-owner projection obligation before canonical-mutating hooks
unknown mutation set != zero mutations
atomic exact-path canonical hook rewrites
boot recovery closes producer obligations only on durable evidence
projection_dirty => full rebuild before currentness claim
filesystem_only projection class is not semantic citizenship
```

The paper distinguishes **frozen architectural law** from **current implementation conformance**. In particular, ADR-MO-003 already says durable adaptive mutation belongs to DeepSleep, but live cascade-time reinforcement currently violates that law and must be repaired before Program II.

### 22.2 Law requiring reconsideration

The current wording of the embedding-obsolescence objective rejects any learned dense embedding dependency in write/index/recall operation. This paper identifies a more precise distinction:

```text
OLD INTERPRETATION
no learned model may participate in memory perception

PROPOSED IF VALIDATED
no learned model may own or persist memory state,
serve as factual admission authority,
create canonical topology,
or receive lived plasticity credit
```

A frozen sensory transducer would be permitted only on the perception side of a hard, versioned discrete boundary. The semantic surface may rank/rout candidates, but canonical verification remains independent and semantic similarity is not experiential credit.

This amendment should **not** be made because the proposal is attractive. It requires Program I geometry/recall/coexistence results, the runtime-seat test, and operator/reviewer approval. Program II validates lifelong plasticity but is not required merely to establish whether the sensory boundary is lawful.

This research also remains subordinate to `19_BUILD_FIRST_EVALUATE_SECOND_FREEZE.md`: benchmark or retrieval-quality results are not allowed to force the unfinished memory organ back toward embedding-era design. Program I informs an operator architecture ruling; even a strong H1 result does **not** automatically amend ADR-MO-008 or compel adoption of the retina. The operator may decide that the sensory-boundary distinction is architecturally justified, reject it, or retain it only as an optional/topical lane despite favorable quality numbers.

### 22.3 Cut 5A implication — proceed, interface-amended

Cut 5A retains its independent purpose and should not be delayed by HQ-NP research. It removes dense vector similarity from the **structural graph**:

```text
remove _build_semantic_similarity_edges
remove SEMANTIC_SIMILARITY
remove SIM_THRESHOLD / SIM_TOPK / MRL_DIM graph dependency
remove stored text_768 vector dependency from graph compilation
keep deterministic structural/entity/workflow/failure topology
```

The Cut-5A TDD plan should additionally state:

1. do not hard-code a future assumption of exactly two planes or one universal geometry/scorer;
2. retire `SEMANTIC_SIMILARITY` from `WEAK_EDGE_TYPES` so no dangling semantic edge class survives;
3. do **not** remove the current `semantic_admission` / `semantic_recall` lane yet, because it is Program-I arm A' and the legacy semantic control;
4. record that the final semantic projection representation is unfrozen pending Program I;
5. the graph compiler consumes canonical files, not ProjectionDelta outbox records; outbox semantics belong to the projector;
6. do not implement HQ-NP, VSA, learned-plane weights, or the new semantic scorer inside Cut 5A;
7. enumerate every current `SEMANTIC_SIMILARITY` consumer and disposition it explicitly: graph compiler/schema/graph-recall/hop/reindex consumers are deleted or rendered dead-by-design with the edge type; `semantic_admission.py` itself remains until Cut 10, but its closure-hop behavior becomes unavailable once the edge type is removed. The TDD report must state that A'-full is now a frozen pre-Cut-5A control and live/post-Cut-5A A' is seed-only.

Thus Cut 5A can proceed as structural cleanup while preserving a neutral interface for the later winning semantic surface.

### 22.4 Cut 13 and Program 0 move earlier

The composite generation manifest and representation-neutral outbox consumer are prerequisites for any live semantic surface. Cut 13 should remain pulled early, and Program 0 should validate publication/crash semantics before runtime HQ-NP is connected to the live organ.

---

## 23. Candidate Revised Architectural Law — Research Only

If the experiments validate the proposal, the following law is a candidate replacement for the overly broad “no learned model” interpretation:

> **Persistent Memory Authority Law:** A learned model may participate only as a frozen, versioned sensory transducer upstream of the discrete memory frontier. Its learned weights may shape perceptual candidate routing, but may not constitute persistent memory state, canonical identity, factual topology, factual admission authority, verifier evidence, or adaptive experiential strength. All lived plasticity occurs in auditable Omega-native state bound to canonical records and exact execution evidence.

This law would distinguish **learned perception** from **learned memory ownership**.

---

## 24. Conclusion

The original embedding system failed Omega not because learned semantic representation was useless, but because the learned embedding engine occupied the wrong architectural role. It was coupled to memory citizenship and repair, sat behind a direct-dial service with retry/backfill semantics, and had to be invoked again for semantic recall. In a multi-lane cognitive system, that made learned perception a shared systems dependency where durable truth and projection correctness were forced to wait on it.

Cut 4 has already removed that dependency from non-identity canonical writes. This paper proposes the next step: preserve the useful pretrained semantic prior while confining it to a **runtime-owned, frozen sensory appliance**. AMD Quark/ONNX deployment produces a versioned INT8 frontier; HQ-NP translates that transient latent into a binary perceptual code; and the dense code lives on a **separate semantic surface** with Hamming/XNOR semantics rather than contaminating the sparse coordinate DME geometry.

Below the sensory frontier, memory authority remains Omega-native: canonical filesystem truth, typed structural graph, sparse DME execution, exact routing receipts, fail-closed verification, and a distinct developmental plasticity system. The retina supplies inherited language invariance. Omega's own experience changes connectivity, accessibility, prediction, context, reconsolidation, forgetting, and unlearning through exact use/contributor receipts and DeepSleep-owned neuromodulated credit.

The resulting design is not a static semantic index. It is a **growing cognitive organ with a frozen sensory cortex**.

Fable's two reviews sharpened the falsification boundary. The math is not the primary risk. The primary risks are density x attribution, lawful recall-time dependency, projector durability, methodology drift in the legacy control, and the current retrieval-keyed reinforcement loop. Accordingly, v0.3 requires a separate semantic scorer/evidence class, Program 0 for outbox/generation correctness, pre-registered A'-full/A'-seed controls, an audit-sampled routing-receipt posture, and Program-II gating on actual-use receipts plus relocation of durable reinforcement to DeepSleep.

If the INT8 semantic manifold collapses, Rademacher codes fail to preserve useful neighborhoods, the semantic plane cannot be audited without making similarity authoritative, the runtime seat recreates multi-lane serialization, or neuromodulated learning cannot improve lived behavior without causal-credit leakage, the proposal should be rejected or narrowed.

If those tests succeed, however, the design achieves the original embedding-retirement objective more precisely: **the learned model is removed from persistent-memory ownership and write-time truth coupling, not from perception itself.** Pretrained linguistic invariance crosses once into a deterministic binary memory substrate, while durable truth and lifelong learning remain auditable Omega state.

---

## 25. Selected Research and Tooling References

1. T. Teyler and J. Rudy. **The hippocampal indexing theory and episodic memory: Updating the index.** *Hippocampus*, 2007. https://consensus.app/papers/the-hippocampal-indexing-theory-and-episodic-memory-teyler-rudy/07d991ad54245fbe803fb419bc011cb0/
2. E. Rolls. **The storage and recall of memories in the hippocampo-cortical system.** *Cell and Tissue Research*, 2017. https://consensus.app/papers/the-storage-and-recall-of-memories-in-the-rolls/bf995adb8b685cd0999fcb53804d9ae2/
3. S. Caron, V. Ruta, L. Abbott, and R. Axel. **Random Convergence of Olfactory Inputs in the Drosophila Mushroom Body.** *Nature*, 2013. https://consensus.app/papers/random-convergence-of-olfactory-inputs-in-the-drosophila-caron-ruta/bfec8172e26151ee815f40b7f97891db/
4. D. Rachkovskij and E. Kussul. **Binding and Normalization of Binary Sparse Distributed Representations by Context-Dependent Thinning.** *Neural Computation*, 2001. https://consensus.app/papers/binding-and-normalization-of-binary-sparse-distributed-rachkovskij-kussul/391ed81d79b756b4aee9195e1bb576e2/
5. K. L. Stachenfeld, M. Botvinick, and S. Gershman. **The hippocampus as a predictive map.** *Nature Neuroscience*, 2017. https://consensus.app/papers/the-hippocampus-as-a-predictive-map-stachenfeld-botvinick/e8d0be24471d52ab8c6fa36984dfdb5d/
6. W. Gerstner, M. P. Lehmann, V. Liakoni, D. S. Corneil, and J. Brea. **Eligibility Traces and Plasticity on Behavioral Time Scales: Experimental Support of NeoHebbian Three-Factor Learning Rules.** *Frontiers in Neural Circuits*, 2018. https://consensus.app/papers/eligibility-traces-and-plasticity-on-behavioral-time-gerstner-lehmann/1d1341ffc747562b85dd37015c1b7459/
7. N. Frémaux and W. Gerstner. **Neuromodulated Spike-Timing-Dependent Plasticity, and Theory of Three-Factor Learning Rules.** *Frontiers in Neural Circuits*, 2016. https://consensus.app/papers/neuromodulated-spiketimingdependent-plasticity-and-frémaux-gerstner/b573c1aaf2655be28fbde9d15948b749/
8. J. L. McClelland, B. McNaughton, and R. O'Reilly. **Why there are complementary learning systems in the hippocampus and neocortex.** *Psychological Review*, 1995. https://consensus.app/papers/why-there-are-complementary-learning-systems-in-the-mcclelland-mcnaughton/5ff3a9a395cd53e3916f7eb6587db410/
9. D. Achlioptas. **Database-friendly random projections.** PODS, 2001. https://consensus.app/papers/databasefriendly-random-projections-achlioptas/99aaa30e4e715667a4a0406caafb1d0f/
10. J. Ji, J. Li, S. Yan, and Q. Tian. **Super-Bit Locality-Sensitive Hashing.** 2012. https://consensus.app/papers/superbit-localitysensitive-hashing-ji-li/849842a2f31c576a96a26589d4e90660/
11. H. Ramsauer et al. **Hopfield Networks is All You Need.** 2020. https://consensus.app/papers/hopfield-networks-is-all-you-need-ramsauer-schafl/dbb2efec3b39548b831feafabfdd4fcd/
12. D. George et al. **Clone-structured graph representations enable flexible learning and vicarious evaluation of cognitive maps.** *Nature Communications*, 2021. https://consensus.app/papers/clonestructured-graph-representations-enable-flexible-george-rikhye/b8a521957bf7536480c478433bd44e38/
13. G. Iatropoulos, W. Gerstner, and J. Brea. **Two-factor synaptic consolidation reconciles robustness with pruning and homeostatic scaling.** *PNAS*, 2025. https://consensus.app/papers/twofactor-synaptic-consolidation-reconciles-robustness-iatropoulos-gerstner/94b5bd1e8ee1590882c15f15c2d7d9b6/
14. AMD. **AMD Quark — ONNX Exporting and INT8 transformer quantization documentation.** Current Quark documentation, accessed 2026-08-16. https://quark.docs.amd.com/latest/pytorch/export/quark_export_onnx.html and https://quark.docs.amd.com/latest/onnx/user_guide_config_description.html
15. ONNX. **QuantizeLinear operator specification.** Current ONNX documentation, accessed 2026-08-16. https://onnx.ai/onnx/operators/onnx__QuantizeLinear.html

---

## 26. Completed Second Fable Review Gate — v0.2

The second review completed against v0.2 HEAD `839c07925584cdd5ee2d5c9837a0bc5d3a9432af` and is preserved at `docs/research/2026-08-16-fable-review-2-semantic-retina-hqnp-neuromodulated-organ.md` (commit `1dec7d80`). Its overall recommendation was **proceed to experiments**: offline Program I and Cut-5A planning may proceed; Program 0 implementation remains sequenced after Cut 5A green; live runtime seating, Program II, and any ADR-MO-008 amendment remain gated. v0.3 integrates the remaining methodological/interface modifications from that review.

The original v0.2 gate is retained below as the audit contract that produced the review.

Fable should review paper v0.2 against the actual repository, the frozen engineering package, the first independent review (`docs/research/2026-08-16-fable-review-semantic-retina-hqnp-neuromodulated-organ.md`), and the preserved Cut-1–4 implementer input in §27. This remains a **read-only architecture review**. Do not implement Cut 5A, Program 0, the retina, HQ-NP, VSA binding, cleanup memory, or plasticity changes as part of the review.

For each item return exactly one disposition:

```text
SUPPORT
SUPPORT WITH MODIFICATION
REJECT
INSUFFICIENT EVIDENCE
```

### 26.1 Verify closure of the first review's blocking corrections

1. **B1 runtime ownership (historical; superseded by ADR-MO-018):** v0.2 placed the retina in omega-runtime; the 2026-08-16 operator ruling now requires the opposite for this component: a bounded brain-resident memory session with no transport/runtime fallback.
2. **B2 live neuromod state:** does v0.2 accurately distinguish ADR-MO-003 target law from the current cascade-time `StrengthIndex.reinforce()` violation?
3. **B3 use gate:** does v0.2 correctly state that `U_a` does not exist, missing relevance/use currently fails open, and Program II is blocked until a use receipt/fail-closed credit path exists?
4. **B4 dense geometry:** does the new separate semantic surface + Hamming/XNOR scorer + independent geometry/evidence class resolve the same-matrix rejection without violating frozen sparse DME semantics?
5. **B5 legacy/current topology:** are the `:52700` protocol, 768-float legacy storage, MRL-256 truncation, Cut-4 non-identity status, identity residual dependency, and DeepSleep drain attribution now stated correctly?
6. **B6 control arm:** is A' (live `semantic_admission` cosine lane) now the correct legacy semantic control for Program I?

### 26.2 Review the new architecture created by reconciliation

7. Is `SemanticExecutionReceipt` a lawful way to prove deterministic semantic routing while keeping a semantic-only candidate topical rather than verified?
8. Does excluding semantic Hamming score from experiential credit adequately preserve CUE != CLAIM and ADR-MO-016 until a second-stage contributor proof exists?
9. Is one composite generation ID across retina/HQ-NP/projection/DME schema/scorer preferable and sufficient for the no-mixed-generation law?
10. Does Program 0 correctly inherit the Cut-4 outbox/dirty/obligation/full-rebuild laws without prematurely choosing a semantic representation?
11. Is Program I now sufficient to falsify the semantic bridge, including float-vs-INT8 geometry, HQ-NP-G vs HQ-NP-R, A/A'/C-G/C-R, coexistence, and runtime throughput?
12. Are the Cut-12/identity and Program-0 gates on H4/live-seat testing correct?
13. Does Program II now have the correct prerequisites for an actually learning/growing organ: use receipts, DeepSleep-owned credit, AssociationStrengthIndex, positive/negative updates, generation attribution, and homeostasis?
14. Is Cut 5A correctly **proceeding interface-amended, not delayed**, without stealing Program-I scope?
15. What is now the strongest remaining argument that the architecture fails?

### 26.3 Required final verdict

End with exactly:

```text
OVERALL RECOMMENDATION
- proceed to experiments
- revise paper then re-review
- reject architecture
- operator ruling required
```

If the recommendation is `proceed to experiments`, distinguish:

```text
Program 0 allowed now?
Program I offline static bridge allowed now?
Cut 5A TDD planning allowed now?
Cut 5A implementation allowed now?
Program I live runtime-seat experiment allowed now or gated?
Program II allowed now or gated?
ADR-MO-008 amendment allowed now or still gated on results?
```

Provide a prioritized list of any remaining blockers versus nonblocking research questions.

---

## 27. Implementer's Input — Fable, Cuts 1–4 build seat (2026-08-16)

**Status:** preserved as an audit appendix. This input was appended after v0.1 froze at `d1666f8b` and after the first independent review (`0df3de21`), then materially integrated through v0.2 and v0.3. It is retained verbatim in substance so later reviewers can distinguish implementer-seat observations from reconciled paper text. Nothing in this appendix was implemented merely by documenting it.

### 27.1 The paper's write-time diagram (§9.1) is one cut behind the frozen transaction

§9.1 draws `atomic canonical write → ProjectionDelta / dirty evidence → async projector`. What is actually frozen
(12 §4a, four Cut-4 reworks) is:

```text
durable WAL open (+ prior snapshot)          ← not durable → the write does not begin
canonical write + encoding                   ← failure → rollback / restore prior
canonical_finalized:<mode>                   ← not durable → rollback / restore + NOT committed
[commit entry closes only on durable delta OR durable dirty marker]
owner projection_obligation entry            ← BEFORE canonical-mutating hooks; hooks skipped if no durable channel
hooks (supersedes / entity-link, atomic exact-path rewrites; unknown mutation set != zero)
final ProjectionDelta(s) OR durable rebuild-required marker
ONLY NOW the obligation closes
```

Please replace the §9.1 sketch with that ladder (or cite 12 §4a). It matters for the paper because §16.2's claim
"record is deterministically reprojected" is only true if the projector obeys the outbox contract in 27.2 — the
delta is a pointer + expected hash, not a payload, and there can be two deltas per hooked write.

### 27.2 The contract the async projector inherits (what Cut 4 actually hands Cut 13/14A)

The paper's projector (§9.1, §15, §16.2) must be written against these frozen facts, none of which are optional:

1. **Latest delta per `memory_id` is authority.** Earlier deltas for the same id are superseded work records
   (a hooked write emits a commit-time `upsert` and a post-hook `upsert`/`supersede`). Compaction is Cut 13 scope.
2. **A delta is `(canonical_path, canonical_hash, coord, commit_seq, sequenced, projection_class)`, never bytes.** The
   projector reads the canonical file at projection time. If `sha256(file) != latest.canonical_hash` the file is
   NEWER canonical truth (a rewrite whose delta was lost or is behind) — project the file, record the projected hash
   in the manifest, and never "fix" the delta. If the file is missing → tombstone class, not an error.
3. **`sequenced=False` deltas are not source order.** Direct gate / boot-sync / API writes carry the last prior epoch;
   only writes inside `core.commit_region.serialized_commit` are stamped by the owning episode. Cut 13 must give
   unsequenced deltas an explicit posture (rebuild-required class or manifest-side ordering) — recorded in 12 §4a as a
   Cut-13 requirement. A semantic plane whose row order is derived from `commit_seq` would silently be wrong for
   every gate write.
4. **`projection_dirty.json` present ⇒ full rebuild before any currentness claim** (LAW-EO-004). The marker is
   whole-profile and deliberately coarse; the projector must not "clear it after catching up on deltas".
5. **`projection_class == filesystem_only` (trait family) deltas are not semantic citizens.** They exist so the
   filesystem projection is honest; the retina must never see them.
6. **Open `projection_obligation` / finalized journal entries are boot's job, not the projector's.** The projector
   should refuse to start a generation while `encoding_journal` recovery has unclosed entries for the profile.
7. **The projector is not a truth authority even for its own manifest**: canonical rebuild is the oracle
   (17 LAW-EO-005); a manifest that disagrees with a full rebuild is wrong by definition.

Add these to §22.1 "preserved without change" — the paper lists "ProjectionDelta / asynchronous projection law" but
the projector-side obligations above are what make §16.2 true.

### 27.3 The retina at recall time is a NEW synchronous dependency; §16.3 needs a receipt, not a policy

Cut 4 removed the learned model from the *write* path. §9.2 puts a learned model back on the *recall* path, which the
paper acknowledges but under-specifies. From the Cut-3 build:

- **The verifier's law is independent re-derivation.** `verify_recall_basis(..., executed_addresses=REQUIRED)` binds
  the basis to the set actually executed, re-derived from query text + active registry + cue policy — never from
  basis metadata, and explicitly **not from `DMEActivationTrace`** (Cut 3 review law: correctness must not depend on
  the trace). A retina cue has no registry addresses to re-derive. Two lawful shapes exist: (a) the run-created
  `DmeExecutionBinding` (`memory/recall_basis.py:506`) carries `(retina_gen, hqnp_gen, query_code_hash)` and the
  verifier's semantic-plane rule is "same generation as the manifest AND code hash reproducible from the query text
  under the pinned generation" — recomputed only on audit sampling, or (b) every recall re-runs the retina inside the
  verifier (double inference, honest but expensive). Pick one in the paper; the review's 26.7 "new basis kind" needs
  this to be well-defined.
- **A semantic-plane hit is topical, never verified** (ADR-MO-005) — agree with 26.7. Note the live precedent already
  has a carrier: since `_merge_canonical_recall` drops DME provenance (DISC-MO register), Cut 3 added the verifier-set
  `recall_basis_validated` mark (`memory/recall_verifier.py:974`) that MRIL honours. §10.2's `V_a` has a concrete
  home today; say so, and keep semantic hits out of it until an exact test exists (26.10).
- **§16.3 degrade must be a RecallBasis-level fact**, not telemetry: a `basis_kind`/lane receipt naming
  "semantic_retina_unavailable" so `unverified admissions = 0` (§19.4) remains provable when the retina is down.

### 27.4 Ordering constraint the paper does not state: Cut 12 precedes any live retina program

Identity (lobe 0) still hard-requires the legacy embedder + self-vector (`memory/encoding.py`, identity gate). Until
Cut 12 lands, a live-seat retina experiment runs **two** learned models resident (`:52700` for identity, the retina
for everything else). H4 ("removes the serialization pathology") cannot be measured honestly in that state, and the
service-health/backfill code the paper wants gone stays reachable. Program I static bridge is unaffected (offline);
Program II and any H4 seat measurement are gated on Cut 12 (or an explicit identity-on-retina decision).

### 27.5 Generation pins multiply; enforce them in one place

§15.3 says the retina/HQ-NP generations "extend" the no-mixed-generation rule. From the seat: after this paper a
recall must prove same-generation across four pins — retina gen, HQ-NP gen (mask + tie rule + zero-norm rule),
projection generation, `DME_SCHEMA_VERSION`/`_artifact_fingerprint`. Recommend the manifest (Cut 13) carry ONE
`generation_id = sha256(canonical JSON of all four)` and that (a) the projector refuses to write a plane whose retina
gen ≠ manifest gen, (b) `DmeExecutionBinding` carries that single id, (c) `sequenced`/`commit_seq` semantics from
27.2(3) are recorded per generation. Four independent pins checked in four places is how mixed-generation execution
sneaks in.

### 27.6 What the seat knows about cost, and a correction to my own numbers

- The Cut-4 evidence latency (native p50 4.9 ms vs legacy 5.6 ms) was measured at `fab71967`, **before** reworks 2–4
  added fsyncs: WAL open + optional snapshot sidecar, obligation entry, atomic-replace temp fsyncs on hooked writes.
  A hooked native write now costs roughly 6–8 fsyncs on a USB-resident tree. Re-measure before any H4 latency claim
  is stated relative to "the write path"; the paper should cite the write-path cost as *to be re-measured at 9cd0346e*.
- The projector must batch outbox reads and canonical reads; per-delta open/read/hash on USB will dominate any retina
  cost at D=16,384. Put "projector I/O per delta" in §18.3 alongside retina latency.
- The retina lives in the runtime seat (review B1) and shares RAM/NPU with the 9B model (262k KV) and resident DME
  planes; `_BITS_CACHE` and plane residency need a stated budget before Program I runtime numbers mean anything.

### 27.7 A discipline the experiment programs should inherit from the four Cut-4 reworks

Every durability claim in this paper (§16.2, §15.2 "projection migration", §19.4 "lost canonical writes = 0") was the
kind of claim that passed a green control suite and then failed a causal, byte-level attack four times in a row. The
programs should therefore include, before results are trusted:

- a **projector crash matrix** — death between reading a delta and writing the plane; torn manifest; generation
  switch mid-batch; dirty marker appearing mid-generation — with before/after canonical bytes compared;
- **stubbable resident sessions** — the legacy `:52700` probe costs 15+ minutes per test run when the engine is down
  (test-tree debt already recorded); the retina session must be injectable/absent in tests from day one;
- **no new truncating writes anywhere in the projector** — use `memory/atomic_replace.py` for manifests/planes.

Concretely: a **Program 0** — outbox consumer skeleton (Cut 14A-lite) + generation manifest (Cut 13, pulled early per
the review) + the crash matrix — is the missing harness. Today nothing consumes the outbox; Programs I/II cannot run
against the live organ without it, and it is representation-neutral (it does not care whether rows are sparse
coordinates or 16K codes), so it does not pre-empt the H1–H3 answer.

### 27.8 Small corrections / additions

- §1: `boot.py` still sets `OMEGA_EMBED_ENGINE=gemma_gguf` (kept for identity) **and** now sets
  `OMEGA_MEMORY_NATIVE_CITIZENSHIP=1`; §16.1's "old failure mode" is historical for non-identity since `9cd0346e`
  (review B5 covers the rest).
- §21.5: agree, and add the Cut-2 lesson — memory ids are DME seat tokens (register), and the registry key tokenizer
  keeps trailing punctuation (`'synapses.'`, DISC-MO): hypervector bits must never be registered as coordinate keys
  and the registry must not be widened to admit them.
- §22.1: add "write-owner projection obligation", "unknown mutation set != zero", "atomic exact-path canonical
  rewrites" and "boot recovery closes entries only on durable evidence" — all frozen at 12 §4a and load-bearing for
  §16.2.
- §22.3 / Cut 5A: agree with 26.12 (interface-amended, not delayed). One addition from the build seat: Cut 5A's plan
  must state that the graph compiler consumes canonical files, not the outbox — the outbox is projector work; a
  compiler that reads deltas would inherit 27.2 obligations it should not own.

### 27.9 One-line position

The paper's diagnosis matches what the four reworks taught: the failures were never the model's semantics, they were
the model sitting where truth had to be durable. Moving it above a hard frontier is right. What decides this is not
HQ-NP-G vs -R; it is (i) whether a 50%-dense code can ever have an exact basis (26.13), (ii) whether the recall-time
dependency can be received without re-creating the service pathology (27.3/27.4), and (iii) whether the projector
that consumes the outbox is built with the same byte-level discipline the outbox itself now has (27.2/27.7).
Proceed with the review's corrections and Program 0 first.
