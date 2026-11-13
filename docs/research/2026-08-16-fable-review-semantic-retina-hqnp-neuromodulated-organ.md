I have everything needed. Here is the review.

---

# Independent Architecture Review — Semantic Retina / HQ-NP / Neuromodulated Memory Organ (paper v0.1, 2026-08-16)

**Reviewer:** Fable (independent). **Scope:** read-only, against repo `d1666f8b` (Cut 4 frozen at `9cd0346e`), the frozen engineering package (`docs/engineering/memory-organ/*`), and three completed live-code audits (embedding path; DME geometry/basis/concurrency; neuromod/credit loop). **Nothing was implemented.**

## 0. Summary verdict

The paper's *diagnosis* is largely right and its *epistemic architecture* (frozen perception above a discrete frontier; plasticity only below it; verifier as final authority) is coherent and consistent with the frozen ADRs. But the paper describes several live mechanisms inaccurately, understates how much of the DME/basis/verifier stack would have to change to admit a dense semantic plane, and — most seriously — presents as "existing architectural law" a DeepSleep-owned plasticity topology that the live code **currently violates in production**. The retina proposal is not ready to alter ADR-MO-008; it is ready to be turned into a bounded experiment *after* the paper is corrected.

---

## 1. Dispositions on the Section 26 contract items

### 26.1 — Legacy embed/backfill serialization & failure diagnosis
**SUPPORT WITH MODIFICATION.**
The three pathologies (serialization, partial citizenship, repeated derivation) are real and evidenced: `pending_embeds.json` + dead-letter (`memory/embed_backfill.py:53-64`), the drain that "would sleep ~18 MINUTES … inside DeepSleep's serialized_commit region" (`embed_backfill.py:474-486`), the 14-attempt/33.5 s local retry envelope (`local_embed.py:725-728`), query re-embedding in `semantic_admission.py:95-103`.
Corrections required:
- `:52700` is an OpenAI-style HTTP POST to a **llama.cpp `llama-server`, direct-dialed, bypassing the omega-runtime gateway** (`local_embed.py:209, 639-642`) — not "HTTP to omega-runtime".
- The DeepSleep drain lives **only** in `deepsleep_loop/loop.py:77-86`; `memory/consolidation.py` has zero embed references and `episode_sram.py` only forwards `skip_embed`. Fix §1 file attribution.
- Cut 4 already removed the embedder from **non-identity** writes (`encoding.py:221-235`, `gates/base.py:266-270`, `promotion.py:816-828`) with a fsync'd `ProjectionDelta` outbox (`projection_delta.py`). The paper's "old failure mode" (§16.1) is partly historical; the residual coupling is (a) identity/lobe-0 writes, which *hard-require* embed + self-vector and may never defer (`encoding.py:214-219, 296-313`), (b) query-time semantic admission, (c) `SEMANTIC_SIMILARITY` compile. Say so explicitly.
- Undersold: the legacy path already has TCP health probing (`local_embed.py:597-621`), sequential batch-16 (`:925-942`), and a cold-path short-circuit that avoids waking the embedder when no stored vectors exist (`semantic_admission.py:88-93`).

### 26.2 — Does a frozen Quark/ONNX retina eliminate the problem or move it?
**SUPPORT WITH MODIFICATION.**
It removes the *service* pathologies (HTTP health, retry storm, gateway bypass, backfill queue) only if the retina is invoked **exclusively by the async projector and by recall-time query perception**, never by the canonical write path — which §9.1 states and Cut 4 already enforces. Two things move rather than vanish:
- **Recall-time coupling stays.** Every semantic query still needs one live retina pass (§9.2). If the session is broken/unloaded, recall degrades to lexical/temporal lanes (§16.3). That is a better failure mode than today, but it is not "no dependency". The paper should stop implying H4 removes the recall-time dependency; it removes the *write-time* one.
- **Identity path.** Nothing in the paper says what replaces `self_vector.py`'s trailing-mean of raw 768-d vectors (`self_vector.py:96-101`, model tag `embed-gemma:300m`). A 16K binary code cannot be averaged the same way. Until Cut 12 defines identity continuity without an embedder, the retina does not eliminate the last embed dependency — INSUFFICIENT EVIDENCE on that sub-claim.

### 26.3 — Is the INT8 frontier technically clean in the current Omega runtime?
**REJECT as stated; SUPPORT WITH MODIFICATION as a proposal.**
- **Live-code conflict with law.** The paper's §4.2/§9 "resident session in the caller" contradicts `AGENTS.md:670-677`: *"The RUNTIME owns model loading; the brain only hits endpoints… Never load one here — grep `InferenceSession` before shipping."* The only in-process `InferenceSession` in the brain today is `memory/dme/npu_backend.py:236,256`, and it is a synthesized bit-matmul proof graph (no `.onnx` files ship), explicitly *not* the production seam (ADR-MO-015; `06_NPU_EXECUTION_SPEC.md` §2.2). A learned 20M-parameter transformer resident in the brain process is exactly the Kokoro-in-cascade mistake that law was written to prevent. **The retina must live in omega-runtime** (or the NPU DME server sibling), reached by a local, non-HTTP-if-you-like but runtime-owned seam. §4.2's "OLD: HTTP; NEW: in-process session" is a false dichotomy — the fix is a *runtime-resident, batched, versioned* transducer with a health surface, not an in-brain session.
- **Quark / 256-INT8 do not exist anywhere in code.** MRL-256 today is a **read-side truncation at graph-compile time** of a stored **768-float** vector (`taxonomy/compiler.py:49-51, 359-360`; `encoding.py:113-119` *raises* on len≠768). The paper reads as if the current path emits 256-d — it does not.
- Recent NPU proof runs did **not** achieve VitisAI placement (profiles show `QLinearMatMul` on `CPUExecutionProvider`, no `vitis_ai_ep` partition), so any assumption that an INT8 ONNX graph will run on XDNA2 in this box is unproven for a *transformer* — the "u8_io only, float out = CPU fallback" constraint (`npu_backend.py:12-16`) applies to every intermediate tensor, not just the output.
- Positive: the generation-manifest list (§4.3) is good and should fold into Cut 13's `NpuProjectionRegistration` (currently a spec-only dataclass, `06_NPU_EXECUTION_SPEC.md:110-125`).

### 26.4 — HQ-NP-G mathematics
**SUPPORT.** Sign-random-projection with Gaussian normals: `P[h_i differ] = θ/π` (Goemans–Williamson / Charikar), so `E[d_H/D] = θ/π`. §5.2's correction (cos 0.92 → 0.128, ≈2,100 differing bits at D=16,384) is arithmetically right. Two nits: (a) write `h = 1[Φz > 0]` rather than `sgn` mapping into `{0,1}`; (b) the estimator variance is `θ/π(1−θ/π)/D` only if hyperplanes are independent, which the fixed 16,384×256 bank satisfies (rank ≤ 256 doesn't matter for the per-bit collision law, but does bound distinct codes — worth one sentence).

### 26.5 — HQ-NP-R as hardware candidate
**SUPPORT WITH MODIFICATION.**
The integerized Rademacher form (512 KiB mask, add/sub reductions, INT32 accumulator — §6.2 correctly rejects INT16 because 256·127 = 32,512 fits but 256·128 = 32,768 does not) is sound. Modifications:
- Rademacher hyperplanes are **not** rotation-invariant; θ/π holds only approximately and fails worst on axis-aligned/sparse latents. Since a Quark INT8 latent *is* axis-quantized, the paper must measure ε(c) on the **quantized** latent, not the float one (it hints at this in §21.2 but should make it a required arm).
- The tie rule matters more than stated: with `z_q` INT8 and Φ∈{±1}, exact zero sums are common for low-norm latents. `GTE_ZERO` will bias density above 50%; use seeded balanced tie bits and *report density* as a gating metric.
- Zero-latent / near-zero-norm inputs (empty strings, stopword-only cues) must be defined out (all-tie → seed pattern) or the code becomes a universal near-neighbor.

### 26.6 — Can 16K dense semantic bits coexist with current DME sparse geometry?
**REJECT for the "same plane / same matrix" reading; SUPPORT WITH MODIFICATION for "distinct plane, distinct scorer".**
Live facts that forbid mixing:
- Rows today carry ~tens of bits in 261,952 (≈10⁻⁴ density; no density constant exists anywhere in code/docs). A 50%-dense 16,384-bit code is 3–4 orders denser per position.
- **The generic-bit DF penalty** (`hop_engine.py:65-66, 627-635`: a bit set in >25% of rows is "generic") would classify **every** hypervector bit as generic and, because `df` is computed over the whole `rows_bits` matrix, would perturb the sparse bits' classification too.
- Score = **AND-popcount overlap only** (`matmul_engine.py:5-6`; no Hamming/XNOR mode exists in `memory/`). For 50%-dense codes, AND-overlap of two *unrelated* codes ≈ D/4 = 4,096, related ≈ higher; you need Hamming (or normalized overlap) semantics — a new scorer, not a new plane.
- Geometry is hard-wired: `validate_weight_entries` rejects any `shape[1] != ENTRY_WORDS` (`geometry.py:80-90`); `validate_payload_operands` rejects width ≠ 261,952 (`:122-154`); `register_plane(path, rows)` has no width/plane-id, one global slot (`matmul_engine.py:48, 69-76`); `HopPlanes` has exactly `strong`/`weak` (`hop_engine.py:299-305`); `_artifact_fingerprint` tracks five files. Also `_BITS_CACHE` (512 MB / 6 entries) would blow instantly with a dense second plane.
- Hard capacity ceiling worth noting independently: page>0 addresses are minted but silently unseatable (`packer.py:65-67`, `lobe_layout.py:40-41`) once a lobe exceeds 21,824 keys, and the registry is globally keyed so a lobe-9 seat can set a lobe-2 bit (`coordinate_registry.py:82-99`). Not the paper's fault, but any density analysis built on "one lobe = one span" is wrong today.
- Additional parity risk: `_npu_overlaps` sends the full 4096-word query **including header words** while numpy strips them (`matmul_engine.py:134-137` vs `:156-162`); the packed query has a nonzero header word 2. Unverified server-side. Any new plane protocol should fix this by construction.
Conclusion: HQ-NP codes must be a **separate semantic plane with its own scorer, receipt, geometry validator, plane id, generation id, and evidence class**. §21.4 correctly flags this as "a major design question"; it should be promoted from a risk to a stated architectural requirement.

### 26.7 — Role/filler VSA vs verifier/basis semantics
**INSUFFICIENT EVIDENCE (mechanism); REJECT (that it plugs into today's basis).**
`RecallBasis` kinds are `lexical | canonical_coordinate | canonical_alias | structural_path | learned_path` (`recall_basis.py:47-55`; the paper's `direct_coord` does not exist), every admissible kind terminates in an exact `address_bit_set` test on the candidate's own row (`:269-280, 301-333`), `query_addresses ⊆ executed` is enforced (`:821-828`), and there is **no float/score field** on the basis. A hypervector or bound-VSA candidate has no valid basis today at three independent points. So VSA is a research arm only; if it wins, it needs a new basis kind (`semantic_code`?) with a defined exact test (e.g. same-generation code hash + Hamming ≤ τ_gen recorded in the trace) and a verifier rule — and by ADR-MO-005 that basis must never make the row *verified*, only *topical*. Note the pre-existing bypass `if item.get("semantic_recall") is True: topical.append` (`recall_verifier.py:968-970`), fed by canonical-lane cosine — the paper should name it, because it is the precedent for how a continuous lane already enters topicality without touching the DME basis.

### 26.8 — Binary cleanup preserving exact contributor attribution
**INSUFFICIENT EVIDENCE.** The paper's own admission (§21.6) is correct: Willshaw/threshold cleanup, like the OR hop plane (`activation_trace.py:13-15`; ADR-MO-016), erases contributor identity. Only "winner-only proof" or a second-stage exact pass over the winner's actual generation rows satisfies ADR-MO-016. Nothing in §8 shows such a pass is affordable at D=16,384 with bounded iterations. Treat cleanup as diagnostic-only until proven.

### 26.9 — Neuromodulated eligibility → RPE → DeepSleep loop vs live code
**REJECT the paper's description of current state; SUPPORT the target topology.**
- **Headline error:** "DeepSleep applies potentiation/depression" is false today. `reinforce_recalled_memories` mutates `StrengthIndex.reinforce()` **at cascade time** at three sites (`cognition_chain.py:2712, 2810, 3081`), and `OMEGA_DOPAMINE_REINFORCE=1` **is set in the live boot profile** (`boot.py:691`) despite the module docstring saying default OFF. Only decay/demotion/`touch()` run at DeepSleep (`episode_sram.py:492-500`). This is finding C-1 in `13_LIVE_REVIEW_CORRECTIONS.md` and an open ADR-MO-003 violation; that doc's line cites (`:2193/:2449`, `boot.py:644`) are stale — three sites, not two.
- `U_a` (used-by-cognition) **does not exist**. Credit set = `episode.auto["memory"]["items"]` = verifier-verified + relevance-anchored rows (`remember.py:72-88, 139-170` → `mril_tools/run.py:60-69`), weighted by recall *score*, and a **missing relevance defaults to full weight 1.0** (`neuromod_reinforce.py:60-70`) — the opposite of fail-closed. Nothing reads the model's response.
- No credit receipt, no `AssociationStrengthIndex` (`memory/association_strength.py` absent — Cut 6 not started), `associations` structurally forbidden on the trace (`activation_trace.py:140-141`), trace is ephemeral (dies with the episode carrier).
- Modulatory vector: only `dopamine` reaches memory credit; `plasticity` is bare `abs(rpe)`; `novelty`/`noradrenaline` live only in the motor/mastery `pathway_credit.py`; `config/plasticity.yaml`'s three-factor gate is dead for memory. Conversely the paper **undersells** the live neuromodulated *encoding* latch (`symphony_neuromod.py:105-136` → `strength_index.py:208-221`, `write_strength *= scale`, `encode_tau *= tau_factor`).
- Attribution: exact along the carrier (`QuerySpec.episode_id` → trace), but the epoch/SRAM resolver is `inferred` under multi-lane and says so (`memory/epoch.py:36-52, 118`). `P_a` therefore cannot be 1 for anything resolved through the fallback.
- Neuromodulated-credit hazards the paper must state: (i) reward-hack loop — dopamine reinforces relevance-ranked items which raises their StrengthIndex which raises their future rank; the boot comment claims the ±0.20 dead band and [0,1] clamp bound it, but no measurement exists; (ii) with `U_a≡1` and missing-relevance→1.0, credit is *retrieval-keyed*, not *use-keyed*, contradicting ADR-MO-011 in spirit; (iii) with a dense semantic plane, candidate fanout rises, so retrieval-keyed credit dilutes further.

### 26.10 — Frozen-prior / plastic-lived split
**SUPPORT.** Coherent, CLS-consistent, matches ADR-MO-007/010/011/013. One requirement to add: the frozen prior's *neighborhood* is still a ranking signal, so "does not become more personally important merely because it is semantically close" (§11.1) must be enforced by keeping semantic-plane score out of the credit weight (i.e. `e_a` must not use the Hamming score as `A_a` for a semantic-plane hit until an exact test exists).

### 26.11 — Live seams that must change if validated
Minimum: `matmul_engine.register_plane` → multi-plane with width/plane_id/generation; new geometry receipt (`geometry.py`); `HopPlanes` + `_artifact_fingerprint` + `DME_SCHEMA_VERSION` bump (`adapter.py:41`); Hamming/normalized scorer + IRON kernel + parity test incl. header-strip; DF penalty scoped per plane; `EVIDENCE_PLANE`/`ActivatedCandidate` new class; `RecallBasis` new kind + `address_bit_set` analogue + verifier rule; `manifest.json` gains `generation_id`/plane hashes (Cut 13); projector (Cut 14A) consumes ProjectionDelta → retina → HQ-NP; runtime seat for the retina (`omega-runtime`, not brain); `_BITS_CACHE` budget; identity path (Cut 12); credit receipt + relocation of `reinforce` into DeepSleep (Cut 7 precondition); `AssociationStrengthIndex` (Cut 6). All are already-scheduled cuts except the multi-plane/scorer work, which is new.

### 26.12 — Cut 5A: unchanged / interface-amended / delayed
**INTERFACE-AMENDED, not delayed.** Cut 5A's independent purpose stands: `_build_semantic_similarity_edges` (`compiler.py:352`), `SIM_THRESHOLD/SIM_TOPK/MRL_DIM`, and the `text_768` read dependency go; the structural graph must compile with no vector fields (Gate D1). Amendments: (1) Cut 5A must not hard-code "two hop planes, one geometry" anywhere new — leave `HopPlanes`/plane registration untouched rather than tightening; (2) it must retire `SEMANTIC_SIMILARITY` from `WEAK_EDGE_TYPES` (`hop_engine.py`) in the same cut so no dangling class remains; (3) it must **not** touch `semantic_admission.py`/`semantic_recall` bypass — that is the interim semantic lane and the control arm for H1; (4) record in its TDD plan that the semantic projection representation is *unfrozen* pending Program I. Delaying Cut 5A gains nothing: no experiment in this paper depends on `SEMANTIC_SIMILARITY` edges existing.

### 26.13 — Strongest argument the architecture fails
Not the math. **Density × attribution.** A 50%-dense 16K code has no per-bit meaning, so a semantic-plane hit cannot be reduced to "these registered addresses were in the executed query and are set in the row" — the exact-bit test that RecallBasis, ADR-MO-016, and the whole eligibility chain rest on. Either you (a) accept a similarity threshold as a "basis", reintroducing continuous authority by the back door (the thing the paper forbids), or (b) sparsify/binarize the code into addressable coordinates, at which point HQ-NP degenerates into a learned-model-derived symbolic cue generator and most of §5–6's geometry argument becomes irrelevant. Combined with a retina that must still be alive at recall time and a credit loop that today is retrieval-keyed rather than use-keyed, the most likely outcome is that the retina improves zero-overlap candidate recall (H1 true) while every semantic-plane hit is permanently `diagnostic`, uncredited, and un-verifiable — i.e. exactly the status of today's `semantic_admission` lane, at higher cost. That is a legitimate result, but it would not justify amending ADR-MO-008.

### 26.14 — Minimum experiment matrix before touching ADR-MO-008 / master plan
1. **Static bridge (offline, no repo change):** float latent vs Quark-INT8 latent neighborhoods (kNN@10 Jaccard, Spearman on pair cosine) for ≥2 candidate small encoders; HQ-NP-G vs HQ-NP-R ε(c) on the *quantized* latent, D ∈ {4096, 16384}; density + exact-zero rate for GTE_ZERO vs balanced ties.
2. **Recall arms on the LongMemEval + Omega paraphrase corpus (§18.2 classes):** A (lawful DME cues), A′ (today's `semantic_admission` cosine lane — the real control, currently missing from §18.1), C-G, C-R; metrics = candidate recall@K, verified recall@K, unverified admissions (must be 0), fanout, query density.
3. **Coexistence probe:** separate-plane Hamming scorer vs same-matrix AND-overlap, with the DF penalty on/off — quantify how far AND-overlap ranking diverges from Hamming for 50%-dense codes.
4. **Runtime-seat throughput:** retina in omega-runtime, batched B∈{1,4,8}, p50/p95 under 4-lane load, vs `:52700` llama-server — the honest H4 test.
5. **Neuromod prerequisites (Program II gate, not retina-dependent):** relocate `reinforce` to DeepSleep behind a credit receipt; add `U_a` (cited-in-response) and make missing relevance → 0; measure positive/negative credit precision and cross-lane leakage on synthetic episodes. Program II must not start on top of the current cascade-time reinforcement.
Only after 1–3 pass do D-R (VSA) and cleanup (§8) become worth running.

---

## 2. Corrections the paper must make (blocking vs non-blocking)

**Blocking (must fix before re-review or before any experiment is written against the paper):**
- B1. §4.2/§9: retina cannot be an in-brain resident session — violates `AGENTS.md:670`; relocate to runtime seat.
- B2. §10.4/§22.1 "existing law": potentiation/depression is cascade-side and live (`cognition_chain.py:2712/2810/3081`, `boot.py:691`); C-1 open; correct and cite as violation-to-fix.
- B3. §10.2 `U_a`: nonexistent; missing-relevance defaults to full credit — state and require fix.
- B4. §21.4 → requirement: separate semantic plane + Hamming scorer + new basis kind + DF-penalty scoping; today's geometry/basis mechanically rejects hypervector candidates.
- B5. §1/§16: correct DeepSleep drain location, `:52700` protocol, Cut 4 status, 768-float storage/MRL read-side truncation, "Quark/256-INT8 exists nowhere".
- B6. §18.1: add arm A′ (live `semantic_admission` cosine lane) as control.

**Non-blocking (research questions):** Rademacher tie policy; zero-norm latent handling; identity path (Cut 12) representation; VSA slot count/crosstalk; cleanup contributor proof; homeostasis rule; retina generation migration cost; header-word parity on `/packed/overlap`; page>0 capacity ceiling and cross-lobe registry leakage (pre-existing, but bound the density study).

---

## 3. Blockers vs non-blockers for proceeding

- **Blockers to any ADR-MO-008 change:** experiments 1–4 above; B1–B6.
- **Blockers to Program II:** C-1 relocation + credit receipt + `U_a`; Cut 6 sidecar. Independent of the retina.
- **Not blockers:** Cut 5A (proceed, interface-amended); Cut 13 manifest (execute early as planned — it is a prerequisite for any generation-pinned semantic plane).

---

## OVERALL RECOMMENDATION

**revise paper then re-review**

Rationale: the direction is sound and the math is right, but the paper mis-states live topology on three load-bearing points (in-brain session vs runtime law; DeepSleep-owned reinforcement vs cascade-time reinforcement; hypervector coexistence with the current basis/geometry), and its H4 arm lacks the correct control. Correct B1–B6, promote §21.4 to a stated requirement, add arm A′, then re-review — at which point the recommendation is expected to become *proceed to experiments* (Program I static bridge first; Program II only after C-1 is closed), with Cut 5A proceeding now as structural-only, interface-amended, and Cut 13 pulled early. No operator ruling is needed for the paper itself; an operator ruling **is** owed separately on C-1 (cascade-time reinforcement vs ADR-MO-003) before Cut 7, as `11_DECISION_REGISTER.md` already records.
