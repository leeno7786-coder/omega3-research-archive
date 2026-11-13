# Semantic Retina / HQ-NP Program I — Operator Amendment

**Date:** 2026-08-17
**Authority:** Operator rulings made after Program 0 / Cut 13 Part B freeze
**Base preregistration:** `docs/research/2026-08-16-semantic-retina-program-i-preregistration.md`
**Architecture paper:** `docs/research/2026-08-16-semantic-retina-hqnp-neuromodulated-memory-organ-paper.md`
**Seat authority:** ADR-MO-018 / `2026-08-16-semantic-retina-brain-residency-operator-ruling.md`
**Program-0 freeze:** `cd2eab89ba96fc30eef427d9c9e4b1d2ba6b8da1`

This amendment changes only the preregistered Program-I experiment choices named below. All other Program-I laws, stop/go gates, safety invariants, representation separation, A′ control requirements, seed rules, artifact requirements, and production non-mutation boundaries remain authoritative.

## A1 — The semantic retina model is locked

Program I uses exactly one encoder:

```text
model_id        tasksource/ettin-17m-embed
snapshot        6132e97e52834eb791fd9333049a1726d01fc898
base_model      jhu-clsp/ettin-encoder-17m
architecture    ModernBERT encoder
hidden_size     256
pooling         mean token pooling
normalization   L2 normalize
similarity      cosine
max_seq_length  1024
```

The earlier requirement to compare at least two candidate encoders is superseded. Ettin is qualification-or-falsification, not a preferred model in a shopping loop.
If Ettin fails the FP32 qualification gate below, Program I stops and the semantic-retina architecture is reopened. No substitute encoder automatically enters the run.

Observed local artifact hashes at lock time:

```text
model.safetensors                    42965efac128bdb06678f2ffbda2054bb31f286f68c10a5eea5796a170fd68b4
tokenizer.json                       b18211964db7d0ae2a8d2c4a4c00e271c13905859726fe980622488d95db75af
config.json                          afb59c561c4685b23b2e71753256f929fd0f79ea57d8429ee9c7feac10599bb0
1_Pooling/config.json                ace9f219846dfea1aad340dd7834ec03a05a32c5841738c383af4f41d59e92fc
modules.json                         84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf
config_sentence_transformers.json    5bd47d0626eedaa34b22396a3dad81ebe0c257f17ffc77f97f0ac58bec4bbc86
```

## A2 — New Gate G0: Ettin FP32 qualification

G0 runs before quantization and before any HQ-NP adoption claim. Thresholds are frozen before observing Program-I corpus results:

```text
ordinary semantic positives:
  intended positive outranks unrelated negative                 >= 95%

zero-token-overlap paraphrases:
  intended match outranks unrelated negative                    >= 90%

hard semantic distinctions:
  correct target outranks role-reversal / negation /
  stale-temporal / polysemy-context / adversarial near-neighbor >= 90%
```
```text
semantic-family retrieval:
  correct-family Recall@10                                      >= 90%

per-stratum floor:
  no major target-bearing semantic stratum                      < 80%

catastrophic collapse:
  valid non-empty zero vectors                                  = 0
  NaN / Inf outputs                                              = 0
  deterministic repeat mismatches                               = 0
```

G0 PASS allows continued Program-I qualification. G0 FAIL stops Program I and returns the semantic-retina design to operator/reviewer architecture review. G0 failure does not authorize threshold revision or encoder substitution.

## A3 — Corpus size, split, and partition leakage

The Program-I corpus is exactly 7,500 cases over the 15 preregistered strata:

```text
per stratum: 500 total = 300 development + 100 validation + 100 locked test
all strata:  7,500      = 4,500 development + 1,500 validation + 1,500 locked test
```

The original 60/20/20 split remains authoritative.

No fact, source memory, paraphrase family, or synthetic template family may cross partitions. Split assignment is frozen and hash-pinned before Ettin sees any Program-I case.
## A4 — Source composition

Across the 7,500 candidate memories, source composition is frozen as:

```text
40% Omega-shaped / Omega research material        3,000 candidates
30% public / LongMemEval-derived material         2,250 candidates
30% controlled adversarial synthetic material     2,250 candidates
```

**AMENDED AND RATIFIED 2026-08-18 (see A9):**

```text
40.0% Omega-shaped / Omega research material      3,000 candidates   (unchanged)
20.7% public / LongMemEval-derived material       1,550 candidates   (-700)
39.3% controlled adversarial synthetic material   2,950 candidates   (+700)
```

Private Omega-derived source material remains local-only. Reproducibility is carried by provenance plus committed hashes/manifests, not by publishing private canonical text.

## A5 — Full-partition retrieval

Every query is evaluated against the complete candidate pool for its own partition. Small per-case candidate bundles are forbidden as qualification evidence.

The corpus contains 7,500 unique candidate memories. There are 7,000 target-bearing query cases, each with one distinct primary target candidate, plus 500 intentionally empty/no-signal query cases with no valid target. The remaining 500 candidates are distractor-only candidates. Hard negatives are real candidates in the same partition, not strings attached only to a test case.

Program-I recall reporting retains the preregistered K values:

```text
K = 1, 5, 10, 32
```

## A6 — Semantic retina render contract

The exact candidate rendering algorithm is versioned as:

```text
omega.semantic_retina.render.v1
```
For each candidate memory:

1. inspect canonical fields in exact order `title`, `summary`, `text`;
2. strip leading/trailing whitespace;
3. omit empty fields;
4. exact-deduplicate repeated field values while preserving first occurrence;
5. join retained values with one LF (`\n`);
6. pass only that rendered text to the pinned Ettin tokenizer;
7. apply the pinned 1024-token deterministic truncation contract.

The renderer excludes memory ID, coordinate, lobe, domain, subtopic, tags, timestamps, provenance, source IDs, graph edges, and all other structured metadata. Query input is raw query text only under the same pinned tokenizer/normalization contract.

## A7 — Model-blind corpus construction

Before corpus freeze, Program-I case authoring and label review may not inspect:

- Ettin embeddings;
- cosine scores;
- nearest-neighbor output;
- HQ-NP codes or Hamming scores;
- any performance-derived recommendation to rewrite a case.

Allowed pre-freeze work is semantic/factual quality review, schema validation, duplicate detection, partition-leakage detection, provenance validation, and target/hard-negative label review.

After the corpus manifest, canonical candidate set, query set, split assignment, renderer version, and hashes are frozen, development execution may begin. Validation and locked-test cases may not be rewritten because of model performance.
## A8 — Updated immediate execution order

The original §17 order is preserved with only the now-resolved choices substituted:

```text
NOW
  1. freeze this operator amendment against the original preregistration
  2. freeze the 7,500-case corpus + split + renderer + A′-full identity
  3. implement the offline Program I-a reference harness only
  4. run Ettin FP32 development/validation and adjudicate G0
  5. if G0 PASS, lock Ettin FP32 execution artifacts
  6. implement/run HQ-NP-G and HQ-NP-R static geometry under the preregistered seeds/dimensions
  7. only after float geometry is understood, build Quark INT8 and measure preservation
  8. run the locked Program I-a gates
  9. if the preregistered static gates pass, implement lab Program I-b/I-c

GATED LATER
  - live semantic-retina seat / production integration
  - Program II neuromodulated learning
```

No production memory behavior changes are authorized by this amendment. Program I remains an offline/reference experiment until a later operator/reviewer architecture ruling explicitly changes that boundary.

## A9 — Two strata move to the controlled synthetic path (ratified 2026-08-18)

`temporal_update` and `zero_token_overlap_paraphrase` are generated on the controlled synthetic path
instead of being authored over natural sources. The omega share stays at 40%; the 700 candidates come
out of the public cohort, which is the material that could not support these two strata.

**Why — measured, not assumed.** Model-blind authoring was run against the frozen job set with the
production retry budget, on a local 4B *and* a 9B author. Four strata authored cleanly
(`adversarial_semantic_near_neighbor`, `domain_term_vs_lay_term`, `morphology_variation`,
`multi_hop_cue` — 813 queries), and `ordinary_synonyms` / `underspecified_cue` sampled 8/8 each. The
other two measured **0%** on both models, for reasons that are properties of the corpus, not the author:

- `temporal_update` — pairs were built from LongMemEval's `question_type == "temporal-reasoning"`
  metadata, which labels the *question*, not a fact→updated-fact relation between two turns. Only
  **5 of 100** natural pairs expressed a real historical→current update; for the rest no honest query
  can express a time role that is not there. (Representative pair: *"excited to make croissants… banana
  bread for the dinner party"* vs *"obsessed with strawberries… baking class yesterday"*.)
- `zero_token_overlap_paraphrase` — the median candidate carried **27.5 content tokens to avoid**
  (p75 49, max 126), and only **11 of 500** were ≤15 tokens and identifier-free; many are code
  references, dataclass field lists, or proper-noun facts. A ≤120-character question that uniquely
  identifies such a fact while avoiding ~28 of its own words does not exist — for a human either.

**Consequence.** Zero overlap is now true *by construction*: each synthetic zero-overlap candidate and
its query are generated from disjoint-stem synonym slots and verified against the real tokenizer before
they enter the corpus, so the stratum tests what it claims to test rather than testing whether an author
complied with an impossible instruction. The authored surface drops from 3,250 to 2,550 jobs.

**What was NOT done:** no validator was relaxed, no query or candidate text was hand-edited, and no
stratum was dropped or resized — all fifteen strata keep their 500/partition-locked counts.

Two separate coverage bugs in the author contract were fixed at the same time (they would have bitten
sound pairs too): the temporal role check accepted only six surface words per role while the instruction
promises "explicit time wording", and the forbidden-token list handed the author stemmer output rather
than the candidate's own words. Both are coverage corrections; the acceptance rules are unchanged.

---

## A10 — G0 is a FIRST-STAGE gate at K=32 (operator ratification 2026-08-18)

**Operator decision:** "go with option 1, gate at K=32."

### What changed

The semantic retina is a first stage that feeds a reranker, so G0 now gates it on
delivering the target into the rerank window rather than on winning a ten-way
contest. **The numeric bars did not move. Only K moved.**

| metric | before | after |
|---|---|---|
| `semantic_family_recall_at_10` ≥ 0.90 | K=10 | `semantic_family_recall_at_32` ≥ 0.90 |
| `stratum_floor` ≥ 0.80 | K=10 | `stratum_floor_at_32` ≥ 0.80 |
| `ordinary_positive_pairwise` ≥ 0.95 | — | unchanged (not K-based) |
| `zero_overlap_pairwise` ≥ 0.90 | — | unchanged (not K-based) |
| `hard_distinction_pairwise` ≥ 0.90 | first-stage gate | **deferred to the rerank gate** |
| — | — | `hard_negative_displacement_at_32` ≤ 0.01 (new) |

K=10 metrics are still computed and reported as diagnostics.

### Why K=32 is the honest window, not a lowered bar

Development measured Recall@32 = 0.840 against Recall@10 = 0.517 on the binding
stratum, and 0.900 against 0.720 on `adversarial_semantic_near_neighbor`. The
encoder reliably places the target in the correct neighbourhood and then loses a
fine-grained contest inside the top ~32 against same-domain siblings. Gating the
first stage on precision-at-10 measures the reranker's job with the first stage's
instrument.

### Why hard_distinction is deferred rather than deleted

Target and hard negative sit at 0.94–0.99 cosine with decision margins of
±0.005–0.03; `role_reversal` pairs have a token-length difference of exactly 0
(identical word multisets, order only) and `negation` differs by the single word
"not". Masked mean pooling is close to order-invariant, so this is beyond any
mean-pooled bi-encoder of this size, not a tuning question. `temporal_update`
sat at exactly 50.00% because its 300 cases are 150 swapped pairs in which
exactly one of each pair must fail whenever the directional cue is unusable.

It is **deferred, not removed**: still computed, still reported, and gated at the
original 0.90 by `RERANK_THRESHOLDS` when a reranker exists.

### The replacement first-stage obligation

Deferring a metric must not mean deleting a constraint, so the first stage takes
on the obligation that actually belongs to it:
`hard_negative_displacement_at_32` ≤ 0.01 — the rate at which the target falls
out of the rerank window **while its own hard negative stays inside it**. That is
the one first-stage failure a reranker cannot repair, because it never sees the
right answer. Measured 0.0020 (3/1500) on development.

Note that co-retrieval was considered and rejected as the wrong metric: a hard
negative *absent* from the window is harmless, it merely removes a distractor.
Measured co-retrieval is 1.0000 for `role_reversal`, `negation` and
`temporal_update` — precisely the strata that fail the pairwise test — so a
reranker is guaranteed to see those pairs.

### Consequence for quantization

The deferred metric is the one with ±0.005–0.03 margins, which INT8 noise would
swamp. The first-stage metrics now gating G0 are rank-window metrics with far
more headroom, so they are the ones with a realistic chance of surviving
quantization. Any future Quark/ONNX work must re-measure the rerank gate
separately rather than assuming it rides along.
