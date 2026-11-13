# Review: DME-Native Associative Memory for Omega 3.0

## Companion notes against `DME_NATIVE_ASSOCIATIVE_MEMORY_ARCHITECTURE.md`

**Status:** Review / counter-proposal for iteration
**Date:** 2026-08-13
**Reviews:** `docs/research/DME_NATIVE_ASSOCIATIVE_MEMORY_ARCHITECTURE.md` @ 198c2828
**Reviewer:** Fable 5, live Linux Omega 3.0 checkout
**Operating assumption (operator, 2026-08-13):** the engine is built against an
**empty memory organ**. There is no corpus to migrate and no legacy tier. First
contact is a design decision, not a constraint.

---

## 0. Summary judgment

The central hypothesis in §4 — *stable coordinates, plastic connectivity* — is
correct and is the paper's most important commitment. Letting Dewey addresses
drift in a learned space would have destroyed deterministic rebuild, provenance,
tile content-addressing, and exact identity in one move. Locating the learning in
the topology *between* fixed addresses preserves every invariant in §1 while
still allowing semantics to be acquired. Nothing below asks to change this.

The safety boundaries in §12 are the right ones, and two are load-bearing rather
than ceremonial: Rule 3 (depression must be as strong as potentiation) and Rule 4
(a compiler-proven relation must never become indistinguishable from a learned
coactivation). §11's sequencing discipline — *make Gemma unnecessary, then remove
it behind measured gates* — is also right and should not be relaxed under
schedule pressure.

This review raises five substantive items. Four are additions or reprioritisations
within the existing architecture. One (§13) argues that a whole section must be
rebuilt, because it was written against a corpus that will not exist.

---

## 1. Verification: the paper's claims about the codebase hold

The paper is a handoff spec, so its factual claims about the tree were checked
before its proposals were argued with. Every load-bearing claim verified:

| Paper claim | Source | Result |
|---|---|---|
| DME entries are 4,096 `uint64`, 3 header words | `memory/dme/lobe_layout.py:18,20` | `ENTRY_WORDS = 4096`, `HEADER_WORDS = 3` — confirmed |
| 21,824 hot coordinate slots per lobe, page 0 | `lobe_layout.py:25` | `PAGE0_SLOTS = WORDS_PER_LOBE * 64  # 21824` — confirmed |
| `uses_embeddings = False` in the DME contract | `memory/dme/geometry.py:36` | confirmed |
| StrengthIndex exposes the operations §5.2 mirrors | `memory/strength_index.py:196,272,300,327,357` | `register` / `touch` / `reinforce` / `set_write_strength` / `forget` — all five present |
| Thought Engine distinguishes graded evidence classes | `memory/dme/hop_engine.py:59-62,104-107` | `direct 1.00 / union 0.80 / strong_hop1 0.60 / weak_hop1 0.25 / strong_hop2 0.20 / weak_hop2 0.05` — confirmed, with concrete weights |
| Reinforcement is dopamine-RPE scaled by recall relevance | `autonomic/neuromod_reinforce.py:13` | `delta = dopamine * relevance_weight * strength_delta_scale` — confirmed |
| Embedding is treated as required atomic encoding | `memory/encoding.py:9,19` | confirmed |
| NPU exactness bounded by u8 output clip | `memory/dme/npu_backend.py:16-17,40` | confirmed, and see §2 below |

**The most consequential verification is `neuromod_reinforce.py:13`.** §5.3 proposes

```
ΔW_ij = η · e_ij · p · d · v
```

and the existing engram rule is already that shape. Phase D is therefore a *seam
extension from memories to edges*, not a new learning system. The paper's own
instruction to "reuse the same thresholding and scale policy rather than
introducing a new learning-rate policy prematurely" is not merely prudent — it is
nearly free, and the review endorses it without qualification.

---

## 2. The 255-bit query budget is the architecture's scarce resource

`memory/dme/npu_backend.py`:

```
QUERY_POPCOUNT_LIMIT = 255      # u8 output stage clips above this
# Exact iff query popcount <= 255.   K = 261,952
```

The paper cites this in §7.1 as an implementation detail of the current placed
configuration. It is more than that. It is the binding constraint on every plane
the paper proposes, and it is radically asymmetric:

```
memory side   261,952 bits per entry     effectively free, baked in DeepSleep
query side          255 bits             paid on every recall, exactness cliff
```

Past 255 the overlap counts do not fail loudly — they **clip**, and the scores
silently stop being exact. That failure mode is invisible to a correctness test
that only checks ranking plausibility.

### Where the paper spends this budget without accounting for it

- **§5.4 quantized excitability planes.** Five tiers (`learned_ltp` …
  `learned_weak`) are five plane families. If recall must probe each, the cue is
  replicated per plane.
- **§5.5 typed plasticity.** Twelve-plus candidate relation classes, each
  potentially its own plane, multiplied by hop depth.
- **§9 fused AIE recall pipeline** with relation-family and Dewey-lobe mapping —
  the fusion is precisely where plane count multiplies.

Taken together the proposal could easily want a cue set an order of magnitude
past 255 while every individual section looks affordable.

### Recommendation

1. Add a **query-bit budget table** to §9. Every proposed plane declares its
   query-side cost, and the sum is checked against 255 as a hard gate.
2. Adopt an explicit design law, which happens to be exactly the shape the
   operator wants for a DeepSleep-heavy system:
   **complexity belongs on the plane, sparsity belongs on the cue.**
   Expensive at write, cheap at read.
3. Prefer designs where added expressiveness costs *plane* bits (free) rather
   than *cue* bits (scarce) — e.g. encode excitability tier by which plane a
   pathway occupies, probed by a single cue, rather than by additional query
   bits per tier.
4. Add a runtime assertion that trips when a composed query exceeds the limit,
   so the exactness cliff can never be crossed silently. §12 Rule 6 already
   establishes the principle that accelerated paths must prove themselves rather
   than be assumed; this is the same principle applied to exactness.

---

## 3. §11.1 — cue canonicalisation should be the primary mechanism, not the seventh

§11.1 lists seven candidate non-neural cue paths for replacing semantic
admission. The seventh is:

> *language-organ cue canonicalisation that outputs symbolic/Dewey cues but does
> not retrieve memory itself.*

This should be promoted to the primary mechanism. It is the only item on that
list that solves the zero-overlap class **on an empty organ**, and §13.3 already
names that class as the hardest acceptance gate.

### The argument

The other six candidates are all *derived from accumulated history*: prior
verified coactivation, learned associative pathways from recall history, typed
graph completion. On a blank store none of them have anything to derive from. A
plasticity system strengthens pathways that have already fired; it has no
mechanism to discover a novel cross-domain link on first contact. That was the
one genuine gap in the architecture, and §11.1 already contains its answer.

The mechanism: at write time, the resident language organ — **already loaded,
already in the box, no second model** — emits canonical cues, aliases and
category terms for the record. *dermatologist* → `{doctor, medical, healthcare,
appointment}`. Those become entity nodes and coordinate bits. At query time the
same canonicalisation converts the query to cues. The model never sees a stored
memory and never ranks anything.

### Why this is stronger than the dense lane it replaces

- **Auditable.** You can read *why* `dermatologist` matched `doctor`. A cosine
  score can never tell you that. This directly serves §12 Rule 5 (plastic state
  must be auditable) and §1 invariant 5 (no learned mechanism may silently
  manufacture a fact).
- **Runs at write / DeepSleep**, which is the operator's standing constraint on
  when the embedder may run at all. The query-side canonicalisation is one small
  bounded generation whose output is symbolic.
- **Fits the §2 budget natively** — it emits a small cue set, not a dense vector
  that must be projected into bits.
- **Needs no embedding model whatsoever**, which is the stated objective of §1.
  This is not a smaller embedder; it removes the category.
- **Works from record #1.** No corpus threshold, no bootstrap tier, no records
  that missed expansion because they predated the mechanism.

### Consequence for §11.2

If cue canonicalisation carries the zero-overlap class, `SEMANTIC_SIMILARITY`
does not need a like-for-like replacement — it needs to *never be built*. On an
empty organ this is free. The paper currently frames §11.2 as retiring an
existing edge type; against a blank store it is simply a compiler feature that is
never switched on, which is a much cheaper proposition and should be stated that
way.

### Risk to name

Write-time canonicalisation makes the language organ a **participant in memory
formation**. §1 invariant 5 must be read carefully here: the cues are *routing
metadata*, never content, and must be stored as such — attributable, auditable,
and rebuildable. A cue must never be able to become a claim. Recommend an
explicit statement in §12 that canonicalised cues are non-authoritative routing
state, and that RecallVerifier remains the admission boundary regardless of how a
candidate was surfaced.

---

## 4. §13 must be rebuilt for an empty organ — and gets better for it

Every baseline in §13.1 (LongMemEval overall and per-category, recall@K,
admission precision/recall, zero-overlap recall, alias recovery, multi-hop
recovery, rebuild time) presumes an existing corpus to measure. Against a blank
store none of them can be taken, and freezing "the current best Gemma-assisted
configuration" measures a system that will not be the starting point.

This is not a loss. It permits a stronger experimental design than the paper
currently proposes.

### Proposed replacement: a genesis curriculum

Replace corpus baselines with a **defined, ordered sequence of memories written
into an empty organ, with probes at fixed checkpoints** (N = 1, 10, 100, 1,000,
10,000). The curriculum is authored once, versioned, and replayed identically
against every ablation in §13.2.

This converts §13.3's zero-overlap suite from a static evaluation into a
developmental one, and answers the question the paper actually needs answered but
never asks:

> **At what N does each mechanism begin to pay?**

- structural association (shared entity, `CO_LOCATED`, `SHARES_TAG`, temporal
  adjacency) is available from N = 2;
- cue canonicalisation is available from N = 1;
- coactivation requires recall history;
- plastic pathway learning requires *outcomes*, which requires episodes, which
  requires a working recall loop.

Those crossover points are precisely the acceptance gates §11 asks for. Without
them, "DME-native reaches parity" has no defined moment at which it becomes true.
With them, the removal decision becomes a measurement rather than a judgement
call.

### Additional consequence

§13.2's ablation list stays valid — it is mechanism-wise, not corpus-wise. But
each ablation should now report *a curve over N*, not a single score. An ablation
that wins at N = 10,000 and fails at N = 100 is a different engineering
proposition from one that is flat, and the paper currently cannot distinguish
them.

---

## 5. Neuromodulation is undefined at low N — §14 needs a genesis epoch

This is the sharpest empty-start hazard, and it arrives through the front door of
a mechanism the paper is right to reuse.

`autonomic/neuromod_reinforce.py` gates on *surprising* outcomes — dopamine as a
reward-prediction error against learned expectation. `strength_index.register()`
samples ambient salience from Symphony at write time. `config/plasticity.yaml`
combines dopamine-gate magnitude, **novelty**, and an inverted-U stress modulator.

On an empty organ:

- there is no expectation baseline, so early RPE is undefined or extreme;
- **everything is novel**, so the novelty term saturates;
- salience is sampled against a Symphony state with no history to normalise
  against.

Left unmodified, the first handful of episodes would write large, poorly-grounded
strength deltas into a topology with almost no structure to damp them. That is a
direct route to permanent attractors formed from N = 3 statistics — exactly the
outcome §12 Rule 3 exists to prevent, reached without violating Rule 3 at all.

### Recommendation

Add an explicit **genesis epoch** to §14, spanning Phases A–B:

1. plasticity learning rates pinned at zero;
2. structural edges and cue canonicalisation active (both are deterministic and
   need no neuromodulation);
3. `DMEActivationTrace` recording throughout, per Phase A, so the pre-plasticity
   behaviour is fully observed;
4. learning rates annealed in as a function of accumulated episode count and
   expectation-baseline stability, not wall-clock or record count alone;
5. an explicit, recorded criterion for exiting genesis.

This also makes Phase A genuinely observation-only in the strict sense, which is
what its name claims.

---

## 6. The structural floor deserves stating explicitly in §11.1

Worth making explicit because it establishes what the learned layer *reinforces*
rather than replaces, and because it is available immediately:

shared entities, shared tags, `CO_LOCATED`, same-episode membership, temporal
adjacency, and `SHARES_DOMAIN` are all derivable at compile time with **no
embedding, no recall history, and no model**. `memory/taxonomy/compiler.py`
already derives entities and claims from canonical records.

That is real associative topology on a blank organ from record #2. §5's plastic
layer should be framed as *modulating access to structure that already exists*,
which is also the safest framing for §12 Rule 4 — structural edges keep their
identity and their provenance, and plasticity only changes how reachable they
are.

---

## 7. Smaller notes

**§5.2 — decide the association store's file now, not "ultimately".** The paper
says the persistence representation "may ultimately deserve its own file for
scale." Edge count grows substantially faster than engram count: structural
relations (`MENTIONS`, `CO_LOCATED`, `SHARES_DOMAIN`, `SHARES_TAG`) are the
dominant families and scale with entities and co-location, not with record count.
Since the organ starts empty, choosing the separate namespace now costs nothing
and avoids a migration later. Recommend deciding it in Phase C.

**§11.3 — removing embedding from atomic encoding is free right now.**
`memory/encoding.py:96` documents `build_embeddings_block` as the *single source
of truth for the on-disk embedding shape*, shared by both write paths. Changing
the record shape normally means migrating every stored record. Against an empty
organ there is nothing to migrate. This is the single strongest argument for
doing the encoding change **first** rather than late in the sequence, and §14
currently defers it to Phase J.

**§11.5 — the same applies to the identity self-vector.** The paper correctly
notes the sparse identity signature "must be evaluated against current
identity-drift tests." On an empty organ there is no drift history to preserve,
so the representation swap carries no continuity cost. Worth stating, because it
removes the one obstacle that would otherwise make §11.5 the hardest replacement.

**§7.1 — the exactness condition should be promoted out of the NPU section.**
It currently reads as a property of the current placed configuration. It is
actually a constraint on the *representation*, and it will outlive any particular
backend. It belongs in §4 or §5 alongside the stable/plastic split.

**§8 / §9 — dirty-tile maintenance interacts with plasticity frequency.** The
content-addressed VAIP scheme in §7.2 recompiles a tile when its hash changes. If
plasticity writes into planes that participate in tile hashing, then every
learning update dirties tiles and the recompile cost tracks learning rate rather
than memory growth. §7.3's separation of compiled program from mutable state is
the stated answer, but until IRON lands, Phases E–F should keep plastic state in
buffers that are *not* part of any tile hash, or the baked-plane design will
fight the learning rule. Recommend stating this explicitly as a Phase E
precondition.

---

## 8. What should not change

- §4's stable/plastic split. This is the paper's core contribution and it is
  correct.
- §12's six rules, particularly 3 and 4.
- §5.3's instruction to reuse the existing thresholding and scale policy — now
  additionally justified by the verification in §1 above.
- §11's ordering discipline: unnecessary first, removed second, behind gates.
- §14's Phase A as observation-only, strengthened by the genesis epoch in §5
  above.
- §7.1's insistence that provider registration is not execution proof.

---

## 9. Suggested revision order

1. Promote the exactness constraint and add the query-bit budget (§2 here).
2. Promote cue canonicalisation to primary in §11.1; restate §11.2 as
   never-built rather than retired (§3 here).
3. Rebuild §13 around the genesis curriculum; make every ablation report a curve
   over N (§4 here).
4. Add the genesis epoch to §14 Phases A–B (§5 here).
5. Move §11.3 (encoding) early in the sequence, since it is free against an
   empty organ (§7 here).
6. Add the Phase E precondition on tile hashing (§7 here).

---
---

# Part II — Review of Addendum A

**Reviews:** `DME_NATIVE_ASSOCIATIVE_MEMORY_ARCHITECTURE.md` @ 9bc85751,
Addendum A (*Factory School, First-Wake Alignment, and Distribution
Architecture*), +704 lines over the reviewed base.

Addendum A resolves the largest unstated problem in the original paper. The core
architecture was written for a single development instance; the product ships
per user. *"Educated before distribution, then personally aligned after first
contact"* is the right answer, and it changes what the rest of the paper is for.

Part I is left standing as written rather than silently edited, because two of
its items were superseded and one was **wrong**. §10 records that honestly. §11
raises what is new.

---

## 10. Standing corrections to Part I

### 10.1 §2 was wrong about the query budget — A.8 is correct

Part I §2 framed `QUERY_POPCOUNT_LIMIT = 255` as a budget **summed across all
planes**, and warned that §5.4's tiers plus §5.5's relation families would
collectively breach it. A.8 corrects this: the limit is **per overlap
query/reduction**, so a sparse cue broadcast to several independently scored
planes is independently exact on each.

Verified in `memory/dme/npu_backend.py`:

> *"Exact iff query popcount <= 255; `overlap()` enforces that instead of
> trusting callers."*
> *"Each output element still reduces over the FULL K inside one shard, so
> sharding never creates partial sums and exactness is unaffected."*

Column sharding does not create partials, so plane count does not accumulate
against the limit. **The correction stands and the addendum's version is the
right one.**

What survives is A.8's own scoping, which is the accurate residual: *any future
fused kernel that concatenates cue families into one reduction must independently
enforce the limit.* §9's fused AIE pipeline is precisely where that happens, so
the constraint should be recorded as a **precondition on fusion**, not as a
global budget. The design law itself — complexity on the plane, sparsity on the
cue — is unaffected and A.8 adopts it.

**Residual cost, correctly relocated:** independent planes are exact but each
costs a dispatch. A.6's six origin classes crossed with five plasticity classes,
if surfaced as separate planes, trades exactness risk for dispatch count and
latency. That is the real budget to watch, and it belongs in §9 alongside the
fusion precondition.

### 10.2 §3 is superseded in the right direction by A.8

Part I argued cue canonicalisation should become §11.1's primary mechanism. A.8
accepts the direction and improves it: rather than the language organ generating
a large cue cloud per query, the **factory cortex grows the expansion on the
memory side**, so the organ emits only a few bounded symbolic entry cues and DME
pattern-completes over pre-grown topology.

That is a better division of labour than Part I proposed and serves the design
law more directly. The risk named in §3 — that cues must remain non-authoritative
routing state and never become claims — still applies and is now *more* load
bearing, because A.6 makes origin and authority explicit product concepts. It
should appear in §12 as a rule.

### 10.3 §4's genesis curriculum is superseded by A.2/A.3

Part I proposed replacing §13's corpus baselines with a genesis curriculum probed
at fixed N. A.2's release curriculum and A.17's graduation report are that idea in
product form, and A.3 adds the discipline Part I did not state: **each lesson must
traverse the real cognition-to-memory loop rather than a factory-only import
path**, which makes the curriculum double as full-system burn-in.

A.3's requirement for knowledge-update, contradiction and negative-control lessons
is a genuine addition and directly answers §13.3's "negative controls where
similar context must not cause admission."

### 10.4 §5's genesis epoch is adopted and relocated — better

A.4 adopts the genesis epoch and moves it **into the factory**, with an
evidence-based exit criterion rather than a wall-clock delay. Part I proposed
calibrating on the customer's instance; A.4 ensures the customer never
experiences the unstable period. Strictly better. See §11.2 for the part that
does not fully survive distribution.

---

## 11. New findings against Addendum A

### 11.1 Coordinate stability must extend ACROSS factory versions, not just within one

A.16 defers the hardest mechanic: *"The mechanics of merging factory and personal
graphs/DME planes remain a research item."* That merge is the load-bearing joint
of the entire distribution model, and A.16 simultaneously promises **versioned
factory-cortex upgrades**.

Those two commitments interact badly unless a contract is stated. §4's stable
coordinate invariant was scoped to one instance. Distribution requires it across
**releases**:

> A coordinate minted by factory cortex vN must denote the same concept in
> vN+1, or be explicitly retired with a recorded supersession — never silently
> renumbered or reused.

Without that, a v2 upgrade invalidates every personal overlay edge pointing at a
moved coordinate. The failure is silent: the edge still resolves to *a*
coordinate, just the wrong concept. This is the same class of hazard §12 Rule 4
guards against within an instance, arriving through the release channel instead.

**Recommendation.** Promote coordinate identity to a versioned, signed part of the
A.5 graduate artifact (`coordinate_registry` is already listed there), and add a
seventh rule to §12: *factory coordinate identity is immutable across versions;
concepts may be added or retired but never re-addressed.* Design this before a v2
exists, because it cannot be retrofitted once overlays are in the field.

### 11.2 A.15's reset list partially undoes A.4's calibration

A.4 moves cold-start instability into the factory. A.15 then resets, at
distribution:

- transient novelty state;
- school-specific recent-success windows;
- eligibility traces;
- transient pressure state.

Those are precisely the statistics A.4's genesis epoch stabilised. The
consequence is that **general physiology ships calibrated while personal
physiology still cold-starts at first wake** — saturated novelty on the
operator's first personal memories, no expectation baseline for personal RPE.

A.14's table half-acknowledges this ("outcome-conditioned personal plasticity:
calibrated/low at factory boot"), but A.4 and A.15 currently read as if the
problem were fully solved.

**Recommendation.** State explicitly that the factory calibrates *general*
physiology only, and that first wake enters a **second, shorter genesis epoch**
for personal plasticity, governed by the same evidence-based exit criterion. The
mechanism already exists — A.4's criteria apply unchanged — it simply needs to be
declared as running twice, over different statistics.

### 11.3 A.2 has no saturation definition, so factory school has no stopping rule

A.2 sets the curriculum target as *"saturation of the general semantic topology
needed to connect compact symbolic cues."* Saturation is never defined, and A.17
commits **every major release** to the same curriculum.

Together those make factory school a build step of unbounded and unpredictable
duration, which release engineering cannot schedule and cannot regression-test.

**Recommendation.** Define a coverage metric and an exit criterion, in the same
spirit as A.4's evidence-based genesis exit. Candidates that fall out of the
existing design: zero-overlap recall on a held-out probe set reaching a
threshold; new-lesson marginal contribution to cue-completion falling below a
floor; coordinate-registry growth rate flattening; A.17's graduation metrics
plateauing across consecutive checkpoints. The point is not the specific metric
but that **graduation must be a measurement, not a judgement call** — the same
standard §11 already applies to Gemma removal.

### 11.4 A.16's encryption model contradicts the product's actual threat model

**Operator ruling, 2026-08-13: nothing in memory is encrypted.** Omega is an
offline-first portable system; no external party has database access. Security is
established at distribution by encrypting source, and on the wire by Tailscale —
a device-level perimeter, comparable to a phone, rather than layered
encryption inside the memory organ.

A.16 currently specifies:

```text
versioned factory cortex
        +
encrypted personal canonical memory
        +
personal plastic association overlay
```

and lists *"encryption focused on the genuinely user-specific layer"* as a
benefit of the separation.

This should be struck. It is not merely unnecessary — it is a **category error
that would cost something real**. Encrypting personal canonical memory while the
factory base stays plaintext puts keys inside the projection-rebuild path: §1
invariant 2 requires graph and DME to be rebuildable from canonical truth, and
§12 Rule 5 requires full rebuild to be able to ignore corrupted projections.
Both become key-dependent operations for no gain under a threat model that
already excludes filesystem access by construction.

**Recommendation.** Remove the encryption clause from A.16 and justify the
factory/personal separation on the grounds that actually motivate it and are
already stated: deterministic provenance, clean personalisation reset, versioned
upgrades, and signed integrity of the distributed baseline. Those are sufficient
on their own. Record the operator ruling alongside, so the clause is not
reintroduced later by someone reasoning from conventional product assumptions.

Note that signing (A.5, A.16) is **not** affected and should be kept — integrity
of a distributed artifact is a different property from confidentiality of local
memory, and the portable/offline model makes signing more important rather than
less.

---

## 12. Revised revision order

Superseding §9 of Part I:

1. Strike A.16's encryption clause; keep signing (§11.4).
2. Add the cross-version coordinate identity rule to §12 and to the A.5 artifact
   (§11.1) — this one gates any future v2 and cannot be retrofitted.
3. Reconcile A.4 with A.15: declare the second, personal genesis epoch (§11.2).
4. Define curriculum saturation and a graduation exit criterion (§11.3).
5. Record the 255-popcount limit as a **fusion precondition** in §9 rather than a
   global budget, and add dispatch count as the real cost of A.6's evidence
   classes (§10.1).
6. Add the cue-non-authority rule to §12 (§10.2).
7. Carry forward from Part I, unaffected by the addendum: move §11.3 (encoding)
   and §11.5 (self-vector) early, and add the Phase E precondition on tile
   hashing.
