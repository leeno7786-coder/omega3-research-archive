# Program I — FP32 development-partition diagnosis (2026-08-18)

Status: **development FAIL, diagnostic only.** Validation has NOT been run and
must not be run until the instrument question below is settled. `locked_test`
remains unopened.

Run: `development`, 4500 candidates / 4500 cases (4200 target-bearing, 300
no-signal), encoder `tasksource/ettin-17m-embed` FP32 snapshot
`6132e97e52834eb791fd9333049a1726d01fc898`, implementation commit `72dd2be5`.

## Result against the frozen G0 thresholds

| metric | value | threshold | verdict |
|---|---:|---:|---|
| ordinary_positive_pairwise | 0.9983 | 0.95 | PASS |
| zero_overlap_pairwise | 1.0000 | 0.90 | PASS |
| hard_distinction_pairwise | 0.7600 | 0.90 | **FAIL** |
| semantic_family_recall_at_10 | 0.9064 | 0.90 | PASS |
| stratum_floor | 0.3767 | 0.80 | **FAIL** |
| zero_vectors / non_finite / repeat_mismatches | 0 / 0 / 0 | 0 | PASS |

Determinism held exactly: two full encoding passes over 9,000 texts produced
byte-identical vectors (`repeat_mismatches = 0`).

## The two failures have different causes

### Failure 1 — stratum_floor: a corpus-generation artifact (ours)

`zero_token_overlap_paraphrase` Recall@10 = 0.3767 and
`adversarial_semantic_near_neighbor` = 0.6400.

Recall@10 by source class: **omega 96.96%, public 99.51%, synthetic 80.79%**.
The decisive control is *within* one stratum: `adversarial_semantic_near_neighbor`
scores **0.97 on omega targets (n=30) but 0.60 on synthetic targets (n=270)**.
Same stratum, same task, same encoder — only the material differs.

Cause, measured: the synthetic cohort is a near-duplicate cluster.

| cohort | n | nearest-neighbour cosine (mean / p50) | mean pairwise cosine |
|---|---:|---:|---:|
| omega | 1800 | 0.6419 / 0.6340 | 0.2164 |
| public | 930 | 0.6099 / 0.6071 | 0.1477 |
| synthetic | 1770 | **0.9610 / 0.9597** | **0.4108** |

Every synthetic candidate has a near-twin at ~0.96 cosine because the generator
varies slot fillers inside a fixed surface template. A synthetic target must
therefore out-rank ~1,769 structural clones to reach top-10. The intruders
confirm it: for missed zero-overlap cases, 87.9% of top-10 intruders are other
synthetic candidates, while 0% share the target's `template_family_id` or
`semantic_family_id` — they are unrelated items that merely *look* the same.

Note the internal contradiction this produces: `zero_overlap_pairwise` is
**1.0000** (the target beats its designated negatives every single time) while
the same stratum's Recall@10 is 0.3767. The encoder ranks correctly; the pool
is pathologically dense. Median rank of a "missed" zero-overlap target is 17
of 4500 — the top 0.4%.

This is repairable at the generator (surface/template diversity, not just slot
diversity). Repair requires regenerating the synthetic cohort, re-authoring its
queries, and issuing a new corpus seal.

### Failure 2 — hard_distinction: a real limit of the encoder

| stratum | pairwise pass | pair structure |
|---|---:|---|
| temporal_update | 50.00% (150/300) | ~4 token difference |
| negation | 60.00% (180/300) | 1 token difference ("not") |
| role_reversal | 70.67% (212/300) | **0 token difference — order only** |
| adversarial_semantic_near_neighbor | 99.33% | — |
| polysemy_context_shift | 100.00% | — |

`temporal_update` sits at exactly 50.00%, and the structure explains why: its
300 cases form 150 groups that share the same two candidates with target and
hard-negative swapped. In **150 of 150 groups exactly one case passes and one
fails**. When the pooled representation cannot use the directional cue, the sign
of `sim(q,X) - sim(q,Y)` is fixed across both queries, so precisely one of each
pair must fail — for any encoder. The 50% is structural, not statistical.

These pairs are well-posed. The queries do carry the cue ("which route does
X use *now after* revision 74" vs "*before* revision 74"), and so do the
candidates. A human answers both correctly.

Direct probe of the mechanism:

| probe | cosine(candA, candB) | decision margins |
|---|---:|---|
| role reversal | 0.9894 | +0.0171 / +0.0098 |
| negation | 0.9804 | -0.0052 / +0.0322 |
| temporal | 0.9404 | +0.0823 / -0.0454 |

The two candidates in a hard-distinction pair are 0.94–0.99 cosine apart. All
discriminative signal lives in the last 1–2% of cosine space, at margins of
±0.005–0.03. This is the textbook limitation of a mean-pooled bi-encoder:
masked mean pooling is close to order-invariant, so "A authorized B" and
"B authorized A" — identical token sets — are nearly the same vector, and a
single "not" moves the vector barely at all.

No corpus change or threshold change fixes this. It is what a 17M-parameter
mean-pooled bi-encoder is.

## Consequence for the planned quantization work

This is the finding that matters most for sequencing. The FP32 decision margin
on hard distinctions is ±0.005–0.03 cosine. INT8 quantization noise operates at
that same magnitude, so `hard_distinction_pairwise` cannot be expected to
survive Quark/ONNX conversion — it is already at 0.76 with the margin intact.
Spending the quantization effort before resolving the hard-distinction question
would be measuring a gate that has already failed in FP32.

## Recommended sequence

1. Repair the synthetic generator's surface homogeneity; regenerate and re-seal
   the corpus. This is defect repair on our side, evidenced independently of any
   threshold.
2. Re-run development. Expect `stratum_floor` to move; expect
   `hard_distinction_pairwise` not to.
3. Only then spend the validation run, which is the single G0 decision surface.
4. Treat hard-distinction as an architecture question, not a tuning question:
   it needs a different mechanism (cross-encoder rerank, or an encoder whose
   pooling preserves order), not a different number.

---

## Iteration 2 — synthetic generator repaired (commit follows this document)

Two defects were found in the synthetic generator and fixed. Neither is a
threshold, model, pooling or query change; both are surface-distribution bugs.

**Defect A — mixed-radix slot walk.** `synthetic_zero_overlap_payloads` advanced
one slot at a time, so consecutive candidates were one-word variants:

```
The archivist stored the ledger in the vault.
The attorney  stored the ledger in the vault.
The chef      stored the ledger in the vault.
```

Every candidate had 5–15 near twins. Recall@10 is a nearest-neighbour question,
so those twins occupied the top-10 slots. Fixed by giving each slot a distinct
**prime** size (11/13/17/19) and indexing with `index % size`: by CRT,
consecutive candidates now differ in all four slots, and two candidates can
share even two slots only if their indices differ by ≥143. The lexicon was
enlarged to 60 disjoint-stem synonym pairs and five function-word-only sentence
frames were added. A test asserts every A-side stem is disjoint from every
B-side stem, so a lexicon collision fails loudly.

**Defect B — one frame per hard stratum.** All synthetic pairs used a single
template in one `Unit-XXXX`/`Relay-XXXX` register. Replaced with four naming
schemes × three templates per stratum. The within-pair contrast is unchanged:
`role_reversal` is still a pure argument swap (a test now asserts both sides use
the identical word multiset), `negation` still differs by one negation marker.

### Measured effect

Nearest-neighbour cosine within the development synthetic cohort, excluding each
candidate's designated hard-negative mate (natural material sits at ~0.63):

| stratum | v1 | v2 |
|---|---:|---:|
| zero_token_overlap_paraphrase | 0.9418 | **0.8004** |
| adversarial_semantic_near_neighbor | 0.9076 | 0.8814 |
| role_reversal | 0.9066 | 0.8841 |
| negation | 0.9048 | 0.8797 |
| temporal_update | 0.9496 | 0.9112 |

G0 on development, v1 → v2:

| metric | v1 | v2 | threshold | |
|---|---:|---:|---:|---|
| ordinary_positive_pairwise | 0.9983 | 0.9983 | 0.95 | PASS |
| zero_overlap_pairwise | 1.0000 | 0.9933 | 0.90 | PASS |
| hard_distinction_pairwise | 0.7600 | 0.7860 | 0.90 | **FAIL** |
| semantic_family_recall_at_10 | 0.9064 | 0.9355 | 0.90 | PASS |
| stratum_floor | 0.3767 | **0.5533** | 0.80 | **FAIL** |

Per-stratum Recall@10 movement: `zero_token_overlap_paraphrase` 0.3767 → 0.5533,
`adversarial_semantic_near_neighbor` 0.6400 → 0.7200, `temporal_update`
0.9167 → **1.0000**, `polysemy_context_shift` 0.9300 → **1.0000**. Natural strata
are unchanged, as expected — the fix touched synthetic surfaces only.

### What still blocks the floor

`zero_token_overlap_paraphrase` at 0.5533 is the binding stratum. Remaining
cause is topical incoherence: the generator combines slots arbitrarily, so it
emits sentences like "The physician purchased the bicycle in the chapel."
Incoherent text embeds into a generic region and therefore clusters. Measured on
matched 8-sentence samples of the same 4-slot shape:

| style | NN cosine | mean pairwise |
|---|---:|---:|
| arbitrary slot combination (current) | 0.5870 | 0.4869 |
| topically coherent scene | 0.4727 | 0.3625 |

Coherence is worth roughly 0.11 of nearest-neighbour cosine. That is real but
probably not sufficient on its own to carry 0.5533 to 0.80, because the stratum
also asks a genuinely hard question of a 17M-parameter encoder: retrieve into
top-10 of 4,500 using synonymy alone, with zero content-token overlap by
construction.

`hard_distinction_pairwise` moved only 0.7600 → 0.7860 and remains a FAIL. This
is the architectural finding from iteration 1 and corpus work will not move it.

### Open question for the operator

Three distinguishable routes remain, and they are not equivalent:

1. **More corpus work** — rebuild the zero-overlap lexicon as topically coherent
   per-domain scenes. Expected to help; unlikely alone to reach 0.80.
2. **Threshold review** — ask whether an 0.80 floor on a zero-content-overlap
   stratum, and 0.90 on hard distinctions, are attainable by any mean-pooled
   bi-encoder of this size. This is threshold tuning and requires an explicit
   operator override of the frozen G0 contract.
3. **Architecture** — accept that hard distinctions need a different mechanism
   (cross-encoder rerank, or pooling that preserves order) and treat G0 as
   having done its job by surfacing that before quantization spend.

---

## Iteration 3 — topical coherence: hypothesis falsified, but a better finding

The zero-overlap lexicon was rebuilt as eight topically coherent domains
(medical, maritime, culinary, legal, horticulture, astronomy, textiles,
geology), each with its own vocabulary and slot sizes 2/3/5/7, so consecutive
candidates land in different domains and consecutive facts within a domain
differ in every slot. Capacity is 1,680 facts.

**The coherence hypothesis did not survive contact with the full corpus.**
Iteration 2's probe compared eight coherent sentences drawn from eight
*different* domains against eight arbitrary ones, and found coherence worth
~0.11 of nearest-neighbour cosine. But in the real corpus ~37 candidates share
each domain, so coherence relocated the crowding inside domains rather than
removing it. The probe was sampled at the wrong density.

Recall@10 on the binding stratum moved 0.5533 → 0.5167 — slightly *worse*.

### spaCy audit — a real contract violation the stemmer missed

`content_tokens` enforces the zero-overlap contract with a light stemmer.
Audited independently against spaCy lemmas (`en_core_web_sm`, already installed
in `.venv-brain`), 3 of 500 zero-overlap cases were violating the contract: the
geology domain paired the place "quarry" (A side) with the action "quarried"
(B side). The stemmer separated them; the lemmatizer correctly merges both to
`quarry`, so those queries shared a word with their target. Fixed, and the
lemma audit is now a permanent test that skips when spaCy is unavailable.

This is worth carrying forward: the light stemmer is the enforced contract, but
it is not a sufficient check on its own for irregular morphology.

### The measurement that matters: Recall@32

| lexicon | R@1 | R@10 | R@32 |
|---|---:|---:|---:|
| v2 arbitrary combinations | 0.213 | 0.553 | 0.733 |
| v3 domain-coherent | 0.100 | 0.517 | **0.840** |

`adversarial_semantic_near_neighbor` shows the same shape: R@10 0.720, R@32
0.900. The v3 **stratum floor at K=32 is 0.840**, which clears an 0.80 bar.

The encoder is therefore not failing to understand the material. It reliably
places the target in the correct neighbourhood and then loses a fine-grained
contest inside the top ~32 against same-domain siblings. v3 is retained over v2
on this basis despite the lower R@10: a corpus whose targets sit inside the top
32 at 0.84 is the better substrate for anything that reranks.

### Both failures now point at one mechanism

- Retrieval: correct region found (R@32 0.840), precision lost inside the top 32.
- Hard distinctions: 0.7860, with pair cosines of 0.94–0.99 and decision margins
  of ±0.005–0.03.

Both are precision-at-the-top problems, and both are what a cross-encoder
reranker exists to solve: it attends jointly over query and candidate, so word
order and polarity survive, and it only has to score ~32 items. A bi-encoder
that delivers 0.84 recall at K=32 is a perfectly good first stage for that.

The FP32 G0 gate as written measures a single-stage bi-encoder at K=10. That is
the honest reason it fails, and it is an architecture question, not a tuning one.
