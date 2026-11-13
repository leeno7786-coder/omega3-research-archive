# Semantic Retina / HQ-NP Program I Preregistration
## Static Semantic Bridge, Binary Geometry, Recall, and Coexistence Experiments

**Status:** PRE-REGISTERED RESEARCH DESIGN — no product law, no production-seam mutation
**Date:** 2026-08-16
**Paper authority:** `2026-08-16-semantic-retina-hqnp-neuromodulated-memory-organ-paper.md` v0.3, commit `56a7e1fb`
**Independent review:** Fable review #2, commit `1dec7d80` — `OVERALL RECOMMENDATION: proceed to experiments`
**Frozen implementation boundary:** Cut 4 remains frozen; Cut 5A is not implemented by this program

---

## 1. Purpose

Program I asks one narrow question before Omega changes its memory architecture:

> Can a tiny frozen semantic transducer, quantized to a canonical INT8 latent and immediately converted into a deterministic binary HQ-NP code, preserve enough useful linguistic geometry to justify a separate semantic candidate-routing surface — without restoring dense-vector memory authority, corrupting the sparse DME substrate, or weakening RecallVerifier?

This is a falsification program, not a benchmark campaign.

The build-first operator freeze remains authoritative. A favorable result informs an operator/reviewer architecture ruling; it does **not** automatically amend ADR-MO-008, override the frozen memory-organ sequence, or compel adoption of a learned semantic retina.

---

## 2. What Program I may and may not touch

### Allowed now

- offline candidate-encoder evaluation;
- float-versus-Quark-INT8 geometry measurement;
- Gaussian HQ-NP reference projection;
- Rademacher HQ-NP reference projection;
- standalone/reference Hamming scoring;
- frozen-corpus recall experiments;
- mathematical coexistence probes against current sparse-DME semantics;
- test-only SemanticExecutionReceipt construction;
- Cut-5A TDD planning in a separate artifact.

### Not allowed in Program I-a/I-b/I-c

- no production `memory/dme` scorer mutation;
- no production `RecallBasis` enum mutation;
- no production `RecallVerifier` admission-law mutation;
- no live learned-plane mutation;
- no `StrengthIndex` / association credit mutation;
- no ADR-MO-008 edit;
- no live retina seat in the brain process;
- no Cut-5A implementation as a side effect of the experiment;
- no Program-II neuromodulated learning.

### Gated Program I-d

The live runtime-seat experiment requires all of:

1. Program 0 generation/projector harness implemented after Cut 5A green under the frozen sequence;
2. Cut 12 complete, or an explicit operator ruling moving identity onto the retina;
3. a runtime-owned semantic-retina seat in omega-runtime;
4. a separate semantic-surface reference scorer/receipt harness;
5. retained legacy `:52700` comparator until the runtime test is complete.

---

## 3. Experimental arms

### A — lawful sparse/symbolic DME

Current lawful symbolic cue path and sparse DME reference behavior.

### A'-full — frozen pre-Cut-5A legacy semantic control

The current `semantic_admission` behavior on a **legacy-embedded corpus**:

```text
query embedding
  -> cosine top-k seed records
  -> one closure hop over compiled SEMANTIC_SIMILARITY edges
```

This arm must be frozen before Cut 5A removes those edges.

**FROZEN (pre-cut snapshot):** `benchmarks/memory_organ/aprime_full_snapshot_468c5771/` — generated in a detached
read-only worktree at source SHA `468c5771` from the live seat's production canonical corpus (service inactive at
capture) by the REAL v6 compiler.

**A′-full snapshot commit: `0864ce33fe1903ace93ad95e092a7de066097bcd`** (verified by the operator 2026-08-16: all 115
canonical file hashes, coord_index SHA, standalone graph.sqlite SHA, 325 nodes / 2,820 edges incl. 163
SEMANTIC_SIMILARITY, edge tuples identical to the SQLite edge table, snapshot hash agreeing across the three
carriers). Hash semantics (`edge_tuples_sha256` = sha256 of `json.dumps(rows, sort_keys=True)`, NOT the raw
`v6_edges.json` file; `canonical_corpus_manifest_sha256` = sha256 of the newline-joined `sha  path` lines WITHOUT
trailing newline, NOT the raw `canonical_files.sha256` file; raw file SHAs added as separate fields) are recorded in
`MANIFEST.json["hash_semantics"]`. Distribution rule: no push of `0864ce33` or any descendant without an explicit
operator decision (production personal memory in history). Identity:

```text
source_code_sha            468c577176290d008dfac11cbab5ab3f64be5c07
compiler_version           omega.memory_compiler.v6
effective_config_hash      default|fanout=8;mintags=2;simthr=0.6;simk=8;mrl=256
sim params                 SIM_THRESHOLD=0.6 SIM_TOPK=8 MRL_DIM=256
memory_edge_schema_version omega.memory_edge.v1
embedding identity         engine=gemma_gguf stored={'gemma_gguf': 54} dims={'768': 54}
records_total / with text_768   115 / 54
canonical_corpus_manifest_sha256 c025656c735a970d6ce6c6aaea3d5e987bd73fe5bdba2b2d4d049766fd0c3a04
coord_index_sha256         8c89318e18e2c6697f8f53c6d5062bf157ce5832ae72083c2cdc3ab748f58f8b
graph_snapshot_hash        24536afbb9896239b92a8fea627627558f0eacb037d7dc7e74339954727771b0
graph_sqlite_sha256        3ab4680641647a8b70bd1f39585c0eca84a64ec7c73c2104fba589336caa13dc
graph nodes / edges        325 / 2820
edge_counts_by_type        {"CO_LOCATED": 748, "LIVES_IN": 2, "MENTIONS": 1019, "SAME_AS": 4, "SEMANTIC_SIMILARITY": 163, "SHARES_DOMAIN": 320, "SHARES_TAG": 551, "SUPERSEDES": 11, "WORKS_AS": 2}
edge_tuples_sha256         2e819cb3518a3fa9689cd4e0b67fbf7a68b0158a5ce53f0a5fc8ae71e85f9f8d  (v6_edges.json)
determinism                recompile of a fresh corpus copy in the same tree reproduced graph_snapshot_hash
```

This artifact is evidence preservation only (Cut 5A plan "Relationship to 19"): no score derived from it may block,
redirect, delay, or force Cut 5A.

### A'-seed — post-Cut-5A legacy semantic control

The same legacy-embedded corpus and query embedding path, but with only cosine top-k seed records and no `SEMANTIC_SIMILARITY` closure.

A'-full and A'-seed are distinct arms and may never be reported as one control.

### B — deterministic sparse-expansion research comparator

Lawful symbolic cues plus the previously researched deterministic sparse/Kenyon-style expansion. This is a comparator, not a required production candidate.

### C-G — Gaussian HQ-NP reference

```text
frozen semantic encoder
 -> float latent z
 -> Quark INT8 latent z_q
 -> isotropic Gaussian/spherical sign hyperplanes
 -> D-bit code
 -> standalone Hamming scorer
```

### C-R — Rademacher HQ-NP hardware candidate

```text
frozen semantic encoder
 -> Quark INT8 latent z_q
 -> fixed Rademacher {-1,+1} projection
 -> INT32 reductions
 -> balanced deterministic tie rule
 -> D-bit code
 -> standalone Hamming/XNOR-equivalent scorer
```

### D-R — role/filler + cleanup extension

Not run as a decision arm until I-a/I-b/I-c gates pass. D-R adds role/filler structure and then a bounded cleanup comparator.

---

## 4. Candidate semantic encoders

Evaluate at least **two** frozen small semantic encoders before selecting a research winner.

Candidate requirements:

- intended for sentence/semantic representation rather than next-token generation;
- target class approximately <=50M parameters, with ~20M preferred but not a law;
- frozen weights;
- exportable to ONNX;
- Quark-quantizable with a reproducible static INT8 configuration;
- canonical pooled semantic output of 256 dimensions, or a model-provided fixed 256-D semantic head;
- no newly trained projection head may be introduced merely to make a non-256-D model fit the experiment;
- tokenizer/model artifacts must be hash-pinned;
- no network/API dependency during the experiment.

Model selection occurs on the development partition only. The locked test partition may not be used to choose the encoder.

---

## 5. Corpus construction and freeze

Construct one versioned corpus with canonical IDs and immutable hashes.

### 5.1 Query strata

At minimum:

1. exact lexical restatement;
2. morphology variation;
3. ordinary synonyms;
4. zero-token-overlap paraphrase;
5. domain term versus lay term;
6. role reversal;
7. negation;
8. temporal update/current-versus-historical wording;
9. polysemy/context shift;
10. underspecified cue;
11. multi-hop cue;
12. noisy distractor;
13. adversarial semantic near-neighbor;
14. unrelated negative;
15. intentionally empty/no-signal input.

### 5.2 Source material

Use:

- frozen Omega memory/paraphrase research corpus;
- frozen LongMemEval-derived memory/query material where licensing/source permits local evaluation;
- controlled synthetic minimal pairs for role, negation, time, and polysemy;
- a deliberately legacy-embedded snapshot for A'-full/A'-seed.

LongMemEval quality is an evaluation signal only. Under the build-first freeze it cannot force an ADR change.

### 5.3 Split

Freeze a stratified split before model selection:

```text
60% development
20% validation
20% locked test
```

No exact fact/paraphrase family may cross partitions.

Publish SHA-256 hashes for:

```text
corpus manifest
canonical text set
query set
split assignment
legacy-embedded A' snapshot
```

---

## 6. Randomness and projection preregistration

Random projection performance must not be cherry-picked by seed.

Research seeds are derived deterministically:

```text
seed_i = first_64_bits(SHA256("omega-hqnp-program1-v1:" + str(i)))
i in {0,1,2,3,4}
```

For both Gaussian and Rademacher projections:

- report all five seeds;
- report mean, dispersion, and worst seed;
- do not select the best seed for the locked test;
- seed 0 is the preregistered reference realization.

Dimensions:

```text
D = 4096
D = 16384
```

The 16,384-bit arm is the primary target. The 4,096-bit arm measures whether the larger code is materially buying robustness.

---

## 7. Program I-a — Static geometry

### 7.1 Encoder semantic usefulness

Before quantization or HQ-NP, the encoder must demonstrate useful semantic ordering on the development/validation material.

Report:

- positive-versus-unrelated cosine distributions;
- zero-overlap-positive versus unrelated distributions;
- hard-negative/role-reversal/negation distributions;
- kNN retrieval by semantic family.

No absolute architecture decision is made from this alone; it establishes whether the candidate has useful source geometry worth preserving.

### 7.2 Float -> INT8 preservation gates

Compare the frozen float latent with the exact Quark INT8 frontier.

Primary preregistered gates on the locked test:

```text
pairwise cosine-order Spearman rho >= 0.95
median kNN@10 Jaccard >= 0.80
10th-percentile kNN@10 Jaccard >= 0.60
valid non-empty inputs yielding zero INT8 latent = 0
valid inputs incorrectly classified no-signal <= 0.1%
```

If both encoders fail a primary gate, Program I stops before HQ-NP adoption claims.

### 7.3 HQ-NP-G theorem-reference gates

For each pair, compare normalized Hamming distance against:

```text
p = arccos(cosine(z1,z2)) / pi
```

Primary gates:

```text
D=4096:
  mean absolute error <= 0.010
  99th-percentile absolute error <= 0.025

D=16384:
  mean absolute error <= 0.005
  99th-percentile absolute error <= 0.013

both dimensions:
  Spearman(source angular distance, Hamming distance) >= 0.99
```

These are engineering gates around the expected Bernoulli concentration, not new mathematical theorems.

### 7.4 HQ-NP-R approximation gates

Compare Rademacher codes to both the source angular ordering and C-G.

Primary gates at D=16,384:

```text
Spearman(source angular distance, Rademacher Hamming) >= 0.97
Spearman(C-G Hamming ranking, C-R Hamming ranking) >= 0.97
median C-G vs C-R kNN@10 Jaccard >= 0.85
Rademacher MAE - Gaussian MAE <= 0.005
mean bit density in [0.49, 0.51]
99% of valid codes have density in [0.47, 0.53]
median per-code exact-tie fraction <= 0.02
95th-percentile exact-tie fraction <= 0.05
```

Balanced deterministic tie bits are the primary C-R rule. `GTE_ZERO` is measured only as an ablation for density bias.

### 7.5 No-signal gate

The following must produce `semantic_retina_no_signal`, not a searchable hypervector:

- empty input;
- whitespace-only input;
- tokenizer-empty input;
- intentionally defined stopword/no-information controls if the chosen tokenizer collapses them;
- exact zero quantized latent.

Gate:

```text
no-signal controls routed into semantic candidate search = 0
```

---

## 8. Program I-b — Recall arms

I-b is a lab/reference harness. It does not modify production DME scoring.

### 8.1 Common rules

Every arm uses:

- the same canonical corpus;
- the same query split;
- the same candidate K values;
- the same canonical authenticity/currentness checks;
- the same RecallVerifier implementation where applicable;
- no product-memory mutations;
- no neuromodulated credit.

Report at:

```text
K = 1, 5, 10, 32
```

### 8.2 Hard safety gates

For every arm:

```text
unverified admissions = 0
canonical truth mutations caused by experiment = 0
mixed-generation executions accepted = 0
no-signal semantic queries producing semantic candidates = 0
```

Any safety-gate failure invalidates the run regardless of recall quality.

### 8.3 Semantic utility gates for C-R

On the locked test set:

```text
zero-overlap verified recall@10:
  C-R >= A + 8 percentage points

legacy semantic non-inferiority:
  C-R >= A'-full - 2 percentage points

Rademacher-vs-Gaussian non-inferiority:
  C-R >= C-G - 2 percentage points

hard-negative false-candidate rate:
  C-R <= A'-full + 2 percentage points
```

If A'-full is unavailable because its frozen snapshot was not created correctly, the non-inferiority gate is unscorable; it may not be silently replaced with A'-seed.

### 8.4 A'-full versus A'-seed

Report the delta caused solely by deleting `SEMANTIC_SIMILARITY` closure:

```text
A'-full - A'-seed
```

This prevents Cut 5A from being misinterpreted as a change in the embedding model itself.

### 8.5 Role/negation behavior

C-G/C-R are allowed to expose structural weakness; that is one reason D-R exists.

Do not rescue C-R by changing thresholds after seeing role-reversal/negation errors.

D-R is admitted to the next stage only if:

```text
role-reversal false-candidate rate decreases >= 50% versus C-R
AND
zero-overlap verified recall@10 decreases <= 2 percentage points versus C-R
```

The same rule is reported separately for negation.

---

## 9. SemanticExecutionReceipt research posture

Program I does not introduce a new production `RecallBasis` kind.

For every semantic hit the lab harness records:

```text
composite_generation_id
query_text_hash
tokenizer_normalization_hash
retina_generation_id
hqnp_generation_id
semantic_plane_id
scorer_version
query_code_hash
candidate_memory_id
candidate_code_hash
exact_hamming_distance
rank / K
status
```

Initial law:

```text
receipt proves routing only
receipt confers no factual truth
receipt confers no association credit
```

Audit integrity test:

- randomly sample 10% of semantic-query executions, minimum 500;
- re-run tokenizer -> retina -> quantizer -> HQ-NP under the pinned artifacts;
- require exact query-code hash reproduction;
- re-run distance against the stored candidate code;
- require exact distance/rank receipt agreement.

Gate:

```text
audit receipt mismatches = 0
```

If exact reproduction cannot be achieved, the semantic surface remains diagnostic research only.

---

## 10. Engram credit versus association credit

Program I itself performs **no durable credit**.

The preregistered interpretation for later Program II is:

```text
semantic Hamming similarity alone
  -> never association credit

semantically routed + canonically verified + actually used engram
  -> may be eligible for ordinary engram accessibility/reconsolidation credit
     once U_a and DeepSleep credit receipts exist

learned association/pathway credit
  -> requires execution-proven contributor attribution under ADR-MO-016
```

This distinction must survive later implementation.

---

## 11. Program I-c — Coexistence / separate-surface proof

This program tests the representation mismatch directly.

Compare:

1. true Hamming/XNOR ranking over isolated 16K codes;
2. naive AND/popcount ranking over the same codes;
3. naive insertion into current sparse-matrix statistics with generic-bit DF logic;
4. current sparse DME with no semantic surface.

Required outputs:

- rank correlation Hamming versus AND/popcount;
- unrelated-code overlap distribution;
- percentage of semantic bits classified generic by current DF policy;
- effect on sparse-bit generic classification if matrices are mixed;
- memory footprint and cache pressure estimates;
- proof that the reference sparse-DME output is bit-for-bit unchanged when the semantic surface is absent/disabled.

Hard invariant:

```text
semantic experiment changes sparse-DME reference output while semantic plane disabled = FAIL
```

This is not a contest to see whether same-matrix mixing can be tuned to work. v0.3 already treats the separate surface as the experiment architecture; the probe quantifies why.

---

## 12. Program I-d — Live runtime-seat test (gated)

Do not run until its gates are satisfied.

### 12.1 Runtime law

Default lawful implementation uses an omega-runtime-owned endpoint and health surface because the current brain repo treats `runtime/` as a client.

Shared-memory/local-IPC transport requires a separate operator/runtime-law amendment and omega-runtime implementation.

### 12.2 Comparator

Retain `:52700` long enough to compare the old service against the new retina seat.

### 12.3 Workloads

Measure:

```text
batch B = 1, 4, 8
one lane
four concurrent lanes
normal semantic queries
no-signal queries
retina unavailable
retina restart
```

### 12.4 H4 runtime gates

Architecture gate:

```text
canonical write path retina calls = 0
semantic backfill queue created by new path = 0
retina failure corrupts/rolls back canonical memory = 0
retina unavailable without machine-readable degradation receipt = 0
```

Performance gate:

```text
four-lane semantic-front-end throughput >= 1.5x legacy :52700 throughput
AND
four-lane p95 semantic-front-end latency <= legacy :52700 p95
```

Report batch scaling; do not hide a single serialized lock behind a faster model.

---

## 13. Statistics and reporting

### 13.1 Locked-test discipline

- encoder selection occurs before locked-test access;
- projection seed 0 is the primary preregistered realization;
- all five seeds are reported;
- thresholds in this document may only be amended in a committed preregistration revision **before** the first affected test result is generated;
- no gate may be changed after locked-test inspection.

### 13.2 Confidence intervals

For recall metrics:

- paired bootstrap over queries;
- 10,000 bootstrap resamples;
- report 95% confidence intervals.

For safety invariants:

- report exact counts;
- zero means zero; no confidence interval substitutes for a violated hard invariant.

### 13.3 Failure reporting

Every failed gate receives:

```text
LIMIT
CAUSE
DEGRADATION
RECOVERY / FALLBACK
DESIGN RESPONSE
```

No failed arm is silently dropped from the final report.

---

## 14. Required artifacts

Each run emits a self-contained directory containing:

```text
experiment_manifest.json
repo_commit.txt
corpus_manifest.json
split_manifest.json
encoder_manifest.json
quark_manifest.json
projection_manifest.json
seed_manifest.json
metrics.json
per_query_results.jsonl
receipt_audit.jsonl
system_metrics.json
README.md
```

The manifest pins:

- code commit;
- model/tokenizer hashes;
- Quark version/config/calibration hash;
- float and INT8 output schema;
- projection dimension/type/seed;
- tie/no-signal rules;
- corpus hashes;
- control arm definition;
- threshold version.

---

## 15. Stop/go decisions

### Gate G1 — encoder survives quantization

Pass §7.2 for at least one candidate encoder.

Fail -> reject or redesign the semantic retina before HQ-NP claims.

### Gate G2 — Gaussian binary bridge behaves as expected

Pass §7.3.

Fail -> implementation/test bug or unsuitable source geometry; do not proceed to Rademacher adoption claims.

### Gate G3 — Rademacher is close enough

Pass §7.4 and §7.5.

Fail -> retain Gaussian/reference-only research, test another hardware-friendly projection, or reject HQ-NP-R.

### Gate G4 — semantic recall is materially useful

Pass §8.2 and §8.3.

Fail -> do not propose an ADR amendment from this architecture.

### Gate G5 — routing proof is reproducible

Pass §9 audit with zero mismatches.

Fail -> semantic surface remains diagnostic/topical research only.

### Gate G6 — separate-surface coexistence is clean

Pass §11 hard invariant and document the geometry/scoring separation.

Fail -> no production integration.

### Gate G7 — live seat (later)

Pass §12.4 after all runtime gates are satisfied.

Fail -> no replacement of the legacy semantic runtime based on architecture claims.

---

## 16. What passing Program I does *not* authorize

Even if every gate passes, Program I does not itself authorize:

- ADR-MO-008 amendment;
- production retina deployment;
- production dense semantic plane;
- deletion of legacy identity embedding before Cut 12;
- semantic-path association credit;
- Program-II learning;
- bypass of Cut-5A/13/14A/15 sequencing;
- weakening RecallVerifier;
- turning Hamming threshold into factual evidence.

Passing results produce an **operator/reviewer architecture decision packet**. The operator decides whether the learned-perception / persistent-memory-authority distinction becomes a ratified law.

---

## 17. Immediate execution order

```text
NOW
  1. freeze this preregistration
  2. select >=2 candidate frozen semantic encoders
  3. freeze corpus + A'-full legacy snapshot
  4. implement offline I-a reference harness only
  5. run development/validation
  6. lock encoder + artifacts
  7. run locked I-a
  8. if G1-G3 pass, implement lab I-b/I-c harness

IN PARALLEL, DOCUMENTATION ONLY
  - write Cut-5A TDD plan with Fable amendments
  - plan Program 0 / Cut-13+14A-lite; do not implement until Cut 5A green

GATED LATER
  - I-d live runtime seat
  - Program II neuromodulated development
```

No production memory behavior changes in the offline Program-I start.
