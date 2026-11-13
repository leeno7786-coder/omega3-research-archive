# Independent Architecture Review #2 — Semantic Retina / HQ-NP / Neuromodulated Memory Organ (paper v0.2, 2026-08-16)

**Reviewer:** Fable (independent). **HEAD:** `839c07925584cdd5ee2d5c9837a0bc5d3a9432af`, branch `docs/memory-organ-engineering-package`. **Scope:** read-only, against paper v0.2 (§26 gate), the first review, the frozen package (`docs/engineering/memory-organ/*`, esp. 11/12/15/17/19/20), and live code. **Nothing was implemented; no subagents, no shell.**

## 0. Summary

v0.2 closes B1–B6 in substance, not just wording: the retina is placed in omega-runtime, the ADR-MO-003 violation is named as a violation, `U_a`/fail-open credit is stated correctly, the dense surface is separated from the sparse `rows_bits` matrix, the legacy topology is now accurate, and A′ is present. Every live-code claim I re-checked holds at HEAD. Three things remain that the paper has not seen: (i) Cut 5A **changes** arm A′ (the live lane does cosine seeds **plus a closure hop over compiled `SEMANTIC_SIMILARITY` edges**, which Cut 5A deletes); (ii) the paper never cites `19_BUILD_FIRST_EVALUATE_SECOND_FREEZE.md`, whose operator ruling bears directly on whether Program-I quality results may ever be an ADR-MO-008 amendment lever; (iii) the `SemanticExecutionReceipt` still leaves the reproduce-vs-re-run choice open that 27.3 asked it to pick. None of these blocks the offline work or Cut 5A; all must land in v0.3 before any recall-arm result is cited.

---

## 1. §26.1 — Closure of B1–B6

**1. B1 runtime ownership — SUPPORT WITH MODIFICATION.**
§4/§4.2/§9.5 now place the retina in omega-runtime, forbid an in-brain `InferenceSession`, and frame the seam as a measurable hypothesis. Consistent with `AGENTS.md:670-678`. Modification: that same law also says *"`runtime/` here is an HTTP client and nothing else."* The paper's "local IPC / shared-memory transport" option is therefore not a free implementation choice; it is a runtime-law amendment plus omega-runtime-side work in a separate repo. Say so, and require the runtime seat to expose the health/degradation surface that §16.3's receipt depends on.

**2. B2 live neuromod state — SUPPORT.**
§10.1/§10.5/§22.1 correctly separate ADR-MO-003 target law (`11_DECISION_REGISTER.md:18-24`, which itself names `neuromod_reinforce` as "the exception to remove/relocate") from the live violation. Verified at HEAD: `cognition_chain.py:2714, 2812, 3083` call `reinforce_recalled_memories`; `boot.py:691` sets `OMEGA_DOPAMINE_REINFORCE=1`. (`13_LIVE_REVIEW_CORRECTIONS.md:45` still cites two stale sites — package drift, not the paper's error.)

**3. B3 use gate — SUPPORT.**
Verified: `autonomic/neuromod_reinforce.py:60-70` returns weight `1.0` for missing/garbage relevance; credit set is `episode.auto["memory"]["items"]` (`:73-85`); no response-use signal exists; `memory/association_strength*.py` absent. §10.3/§19.1 correctly block Program II on a fail-closed use receipt.

**4. B4 dense geometry — SUPPORT WITH MODIFICATION.**
The separate surface (§3, §9, §21.4) resolves the same-matrix rejection: DF penalty (`hop_engine.py:65-66, 627-633`), AND-popcount scorer (`matmul_engine.py:5`), single-slot `register_plane` (`:69`), `ENTRY_WORDS=4096` (`lobe_layout.py:18`) are all left untouched by design. Modifications: (a) "without violating frozen sparse semantics" is true only if the paper also names the *shared* seams a second surface must still touch — `register_plane`, `_artifact_fingerprint` (`adapter.py:349`), `DME_SCHEMA_VERSION` (`adapter.py:41`), `HopPlanes` (`hop_engine.py:300`), `_BITS_CACHE` budget, `RECALL_BASIS_KINDS` (`recall_basis.py:47-55`, needs a `semantic_code` kind), `DmeExecutionBinding` (`recall_basis.py:506`); (b) the dense surface is not covered by frozen `02`/`06` and must be an explicit spec/ADR addition if validated (Cut 15 "multi-plane packed runtime protocol" is the natural home). State both.

**5. B5 legacy/current topology — SUPPORT.**
All verified: `:52700` = OpenAI-style `/v1/embeddings` on a direct-dialed llama-server (`local_embed.py:209, 639`); 768-float storage + MRL-256 read-side truncation (`compiler.py:49-51, 358-360`); drain in `deepsleep_loop/loop.py:77-86`; Cut 4 non-identity native path (`encoding.py:221-235`); identity hard-requires embed + self-vector (`encoding.py:214-219, 300-315`); `boot.py:702/708` sets both `OMEGA_EMBED_ENGINE=gemma_gguf` and `OMEGA_MEMORY_NATIVE_CITIZENSHIP=1`.

**6. B6 control arm — SUPPORT WITH MODIFICATION.**
A′ is the right control, but v0.2 mis-describes it as "cosine lane" only. Live A′ = top-k cosine seeds (`semantic_admission.py:70-125`) **plus** a closure hop over compiled `SEMANTIC_SIMILARITY` edges (`semantic_admission.py:15, 140-162`; `canonical_recall.py:1344`). Two consequences the paper must state: (a) Cut 5A deletes those edges, so post-5A A′ is seed-only — define A′-full (pre-5A snapshot) vs A′-seed (post-5A) and pre-register which one is the control; (b) A′ returns `no_record_vectors` for any record without `text_768` (`:87-92`), and post-Cut-4 native non-identity writes carry no vector — the A′ corpus must be a legacy-embedded corpus (or written with native citizenship off), and the paper must say so or A′ silently measures nothing.

## 2. §26.2 — New architecture created by reconciliation

**7. SemanticExecutionReceipt — SUPPORT WITH MODIFICATION.**
Lawful in principle: a routing proof that never confers truth is exactly the `DmeExecutionBinding` precedent (`recall_basis.py:506, 694-701`, generation-stale → rejected). Modifications: (a) 27.3 asked the paper to *pick* between "recompute on audit sampling" and "verifier re-runs the retina"; §9.4 still says "or". Pick, because Cut 3's law is that verifier correctness must not depend on the trace: under audit-sampling, a `semantic_code` basis is **not** Cut-3-admissible for topicality on unsampled queries and must enter topicality only through the existing lane-flag path. (b) Wording: "topical, never verified" conflates two things. Live precedent (`recall_verifier.py:929-976`) operates on rows that already passed canonical authenticity; the `semantic_recall` flag (`:968-970`) grants *topicality without lexical overlap* to a **verified** row. Say "the receipt confers routing/topicality only; canonical verification is independent and may still verify the record." (c) Add `query_text_hash` + tokenizer-normalization hash to the receipt (needed to reproduce the code) and a `semantic_retina_no_signal` variant.

**8. Excluding Hamming from experiential credit — SUPPORT WITH MODIFICATION.**
Consistent with ADR-MO-016 (`11:74-88`: distance is not a contributor proof) and ADR-MO-006. One clarification: ADR-MO-016 permits **engram-level** credit for a record that demonstrably participated in cognition regardless of route. The paper's "zero durable association credit" is right; §19.5's "credit to semantic-only similarity = 0" should say *association* credit, and state whether a semantically-routed, verified, actually-used engram (U_a=1) may receive engram credit. Otherwise a reader will implement it as a blanket ban and lose lawful engram learning.

**9. One composite generation ID — SUPPORT WITH MODIFICATION.**
Preferable, and necessary. Not yet sufficient: if the composite hashes sparse and dense components together, the two surfaces must publish in lockstep (one generation switch) or the composite must be a tuple of per-surface generations plus a compatibility assertion — otherwise a semantic-only reprojection (§15.2) invalidates every sparse binding. State the publication cadence rule. Also state that the composite **supersedes** today's `projection_generation` (= graph/DME snapshot hash), rather than sitting beside it.

**10. Program 0 — SUPPORT WITH MODIFICATION.**
Correctly inherits `12 §4a` (ladder verified at `12:173-190`; delta fields `12:132-151`; `pending_projection_deltas` is read only by health today, `memory/api.py:2285` — nothing consumes the outbox). Representation-neutral ✓. Modifications: (a) sequencing — per `15:27-28`, `17:438-461`, `19:130-135`, Cut 13's prerequisite is Cut 5A green and 14A follows 13; Program 0 = 13 + 14A-lite, so its *implementation* follows Cut 5A unless the operator re-sequences (planning may start now); (b) the §17.4 minimum contract omits the explicit `sequenced=False` posture that `12:153-156` records as a Cut-13 requirement — add it.

**11. Program I sufficiency — SUPPORT WITH MODIFICATION.**
Arms and metrics are the right set. Modifications: (a) partition Program I into I-a offline geometry (no repo change), I-b recall arms (needs a lab harness with a reference Hamming scorer + verifier over a canonical corpus — do not touch production DME seams), I-c coexistence probe (offline math), I-d runtime seat (gated); (b) pre-register numeric falsification thresholds (what Jaccard/Spearman/ε counts as "collapse") before running — §18.7 lists shapes, not values; (c) A′ per item 6.

**12. Cut-12/identity and Program-0 gates on H4/live seat — SUPPORT.**
Correct: identity still requires the legacy embedder, so a live seat runs two learned models; H4 unmeasurable until Cut 12 or an identity-on-retina ruling. Add: also gated on the runtime-side seat existing (omega-runtime repo) and on Cut 10 not yet having removed `:52700` (it remains the comparator until then).

**13. Program II prerequisites — SUPPORT WITH MODIFICATION.**
List is correct. Add: map each to its vehicle (Cut 6 sidecar, Cut 7 receipt + relocation, Cut 8 consolidation, Cut 9 planes) and the operator ruling owed on C-1/RQ-MO-014 (`11:235`); state that epoch resolution `inferred` under multi-lane ⇒ `P_a = 0` (already the Cut 7 rule in `17:492`).

**14. Cut 5A interface-amended, not delayed — SUPPORT WITH MODIFICATION.**
Amendments 1–6 are right and match `17:421-436`. Add amendment 7: enumerate every `SEMANTIC_SIMILARITY` consumer (`compiler.py`, `schemas.py:135`, `recall.py:241, 582`, `recall_graph.py:234, 349`, `hop_engine.py:93`, `reindex.py:165`, `semantic_admission.py:140-162`, verifier comment `recall_verifier.py:265`) and decide per-site: delete with the edge type, or leave dead-but-harmless for Cut 10. Record explicitly that Cut 5A degrades A′ to seed-only. Cut 5A must not delete `semantic_admission.py` (Cut 10 scope).

**15. Strongest remaining argument the architecture fails — (assessment, no disposition).**
Unchanged in kind, sharper in form: **density × attribution + methodology.** A 50%-dense code has no exact per-bit basis; the receipt proves routing only; semantic hits are permanently topical and uncredited — functionally today's `semantic_admission` lane at higher cost and with a *new* synchronous learned dependency at recall time. That outcome contradicts ADR-MO-008 as written ("no embedding required for … recall"), `17 §2.4/2.6`, and `19 §1(3)`. And `19 §2` rules that "no performance/quality score is allowed to force the unfinished architecture back toward embedding-era design" — so even a strong H1 result cannot, on its own, amend ADR-MO-008. The paper does not cite `19` at all; it must, and it must say that Program I is research that informs an operator ruling, not evidence that compels one.

---

## 3. Blockers vs non-blocking

**Blockers to citing any recall-arm result / to v0.3:** A′ definition + corpus (item 6); receipt shape choice (item 7); `19` reconciliation and ADR-MO-008 posture (item 15); composite-ID cadence rule (item 9); Program 0 sequencing + unsequenced posture (item 10).
**Blockers to Program II:** C-1 relocation, `U_a`, Cuts 6–8, operator ruling — unchanged.
**Non-blocking research questions:** tie rule/zero-signal; Rademacher ε on the quantized manifold; VSA slot crosstalk; cleanup contributor proof; homeostasis rule; `_BITS_CACHE`/plane residency budget; header-word parity on `/packed/overlap`; page>0 capacity ceiling.

---

## OVERALL RECOMMENDATION
- **proceed to experiments**

Program 0 allowed now? — **Planning/TDD yes; implementation after Cut 5A green** per the frozen 15/17/19 sequence (operator may re-sequence).
Program I offline static bridge allowed now? — **Yes**, offline, no production-seam changes, thresholds pre-registered.
Cut 5A TDD planning allowed now? — **Yes** (it is `15`'s NEXT TO PLAN), with amendments 1–7 above.
Cut 5A implementation allowed now? — **Yes, once its TDD plan is written and reviewed per `20`**; not before the plan.
Program I live runtime-seat experiment? — **Gated**: Program 0 + Cut 12 (or identity-on-retina ruling) + runtime-owned seat + separate-surface harness.
Program II? — **Gated**: C-1 relocation behind DeepSleep credit receipt, `U_a` fail-closed, Cut 6 sidecar, Cuts 7–8, operator ruling.
ADR-MO-008 amendment? — **Still gated on results AND an operator ruling**; under `19` results alone cannot amend it.
