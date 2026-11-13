# Two-Way Cognition: A Research Paper Mapped to Omega 3.0

**Working draft · session of 2026-04-28 · authored as Claude's half of a parallel research effort with Noah**

---

## Abstract

Modern AI systems are sophisticated stimulus-response engines. Even
"agentic" frameworks fundamentally wait to be asked. The unsolved
problem at the frontier of AI is **curiosity** — the proactive,
outward-facing cognitive want that drives a system to *seek* rather
than merely *answer*. This paper surveys the biological, neuroscientific
and computational literature on curiosity, motivation, metacognition,
and conversational joint action, and maps each principle to a concrete
component of Omega 3.0's existing or planned architecture. It argues
that Omega's persistent memory substrate, autonomic signal layer,
and reflective loop architecture make it unusually well-suited to
build genuine two-way cognition — *if* a small set of currently-missing
primitives are added in the right order, on the constrained tiny-model
hardware Omega already runs on.

The central claim: real cognition is not a property of the model. It
is a property of a **system that maintains a model of what it does
not yet know**, can **generate questions that would close those gaps**,
and has a **way to seek those answers outward** — to the user, to
the world, or to its own deeper reflection.

---

## 1. Why this is the unsolved problem

### 1.1 Reactive systems are deaf to absence

A pretrained language model knows what was in its training corpus.
It does not know what *isn't* in its training corpus. When asked
about something it doesn't know, it can be prompted to say "I don't
know" — but it has no record of the moment, no follow-up, no
intention to find out later. The unknown is not represented at all.

Most agentic frameworks (ReAct-style tool use, AutoGPT lineage,
function-calling with router LLMs) inherit this limitation. They are
*goal-completing* systems: given a task, find tools and produce
output. Remove the task and they are inert. None of them maintains
an internal state of *unfulfilled wanting*.

### 1.2 What's missing is not a feature, it's a substrate

You can prompt a model to "ask a curious question about the user"
and it will generate something that looks curious. That output is
*about* curiosity; it is not produced *by* curiosity. The difference
is that real curiosity has four properties simultaneously:

1. **Persistence** — the question outlives the moment it was asked
2. **Specificity** — it points at a particular gap, not "stuff in general"
3. **Volition** — the system decided to ask, not because it was prompted to
4. **Resolution-seeking** — the system tracks whether the question got answered

A system that can produce all four exhibits curiosity. A system that
produces a synthetic-curious-sounding sentence on demand does not.
The distinction is mechanical, not philosophical.

### 1.3 Why tiny models can be enough

The instinct in current AI is "scale solves it." Empirically, scale
has not produced curiosity in any model tested at any size. GPT-4 does
not wonder about you between sessions. Claude does not save up
questions to ask next time. The 1T-parameter frontier models are not
more curious than the 1B-parameter ones — they are more eloquent
about pretending to be.

This means scale is not the bottleneck. **State is the bottleneck.**
A 1.7B model on top of a substrate that maintains durable
question-state, gap-accounting, and outward routing can produce
behavior that no stateless 1T model can produce. This is exactly
Omega's architectural bet — the contract names it as "Intelligence
Emerges From Architecture, Not Model Size."

---

## 2. Biological and neuroscientific foundations

### 2.1 Information-gap theory of curiosity

Loewenstein (1994, *Psychological Bulletin*) framed curiosity as the
response to a **perceived gap between what one knows and what one
wants to know**. Critically, the gap must be *perceived* — an organism
unaware of its own ignorance feels no curiosity. This locates
curiosity downstream of metacognition: you must know what you don't
know.

Berlyne (1960) earlier proposed **collative variables** that drive
curiosity: novelty, complexity, conflict, and uncertainty. Berlyne's
contribution is that curiosity isn't just about absence (a missing
fact) but about *structural properties* of the input: contradiction
between two facts, surprising complexity, novel patterns.

Kidd & Hayden (2015, *Neuron*) reviewed the modern neuroscience of
curiosity, distinguishing **perceptual curiosity** (transient, driven
by novel sensory input) from **epistemic curiosity** (sustained,
driven by knowledge gaps). Both involve dopamine but along different
circuits.

**Mapped to Omega:**
- Perceived gap = a memory query that returns no relevant records →
  this is *detectable* in `auto.reflect` if we instrument it
- Collative variables (contradiction, novelty) = already part of the
  memory contract (`contradiction_state` field in schema) but unused
  at runtime
- Epistemic vs perceptual = autonomous-loop curiosity (epistemic,
  long-arc) vs brain-loop curiosity (perceptual, in-cycle)

### 2.2 Dopamine and reward-prediction error

Schultz et al. (1997) established that dopamine neurons fire on
**reward prediction error** — the difference between expected and
actual reward, not absolute reward. This is the foundational signal
for learning by surprise.

Dayan & Niv (2008) extended this to *information* prediction error:
dopamine spikes when an organism learns something unexpected, even
without immediate reward. Curiosity is, in this view, a hijack of
the reward system: the brain treats *closing an information gap* as
intrinsically rewarding.

Schmidhuber (1991, 2010) formalized this as **intrinsic motivation
from compression progress**: a curious agent rewards itself when its
internal model gets simpler (i.e., when it learns a structural
regularity). This is the basis for most computational curiosity work
in AI (see §3.2).

**Mapped to Omega:**
- Omega's `autonomic/dopamine/` module already exists — it computes
  a dopamine signal per cycle and writes to memory (lobe 6 / observations)
- It's currently **write-only**: no consumer reads it to drive
  behavior. The contract explicitly says autonomic signals are
  observational, with Global as the only legitimate consumer
- The crawl: have Global use the dopamine trend as the *curiosity drive
  intensity*. High dopamine = system is learning, keep doing what it's
  doing. Sustained low dopamine = no learning happening, push for
  more questions / different routing

### 2.3 Active inference and the predictive brain

Friston's free-energy principle (2010, *Nature Reviews Neuroscience*)
casts the brain as a **prediction machine** that minimizes the
discrepancy between its generative model and incoming sensation.
Crucially, the brain has two ways to reduce prediction error:

1. **Update the model** to match the world (perception)
2. **Act on the world** to make it match the model (action)

This is **active inference**. An organism doesn't just passively
receive — it *acts to confirm or refine its predictions*. Looking
toward an unexpected sound, asking a clarifying question, opening
a drawer to verify what's inside — all are active-inference moves.

Clark (2013) summarizes: "the predictive brain is a busy, enactive
brain — not a passive surface for the world to write on."

**Mapped to Omega:**
- The brain loop currently is *passive surface*: input arrives, get
  processed, output goes back. There's no "act on the world to
  confirm a prediction."
- Active inference would mean: when reason encounters uncertainty,
  the system *acts* — asks the user a clarifying question, fetches
  external info, queries memory more deeply. We already have the
  `proposed_actions` field in `ReasonOutput`; we have a tool dispatch
  for `web_fetch`. We don't have a "ask the user" action.
- The **proactive output channel** (Omega-initiated brain cycles)
  is exactly the missing piece for active inference at the user surface.

### 2.4 Memory consolidation and sleep

Stickgold (2005, *Nature*) and Diekelmann & Born (2010, *Nature
Reviews Neuroscience*) reviewed the role of sleep in memory
consolidation. Two stages matter:

1. **Slow-wave sleep (SWS)** consolidates declarative memory through
   hippocampal-neocortical replay. Patterns of activity from the
   day's experience are *replayed at compressed speed*, transferring
   them from short-term hippocampal storage to durable cortical
   representation.
2. **REM sleep** consolidates procedural and emotional memory, and
   appears to be when *novel associations* across previously
   unconnected representations form.

Crucially, sleep is when **questions consolidate too**, not just
answers. Open problems often see breakthrough during or just after
sleep — the structural reorganization makes connections that the
awake brain couldn't surface.

**Mapped to Omega:**
- Omega has a `Deep Sleep` layer in its 5-layer stack, designed for
  exactly this consolidation. The contract says:
  > "Only Deep Sleep restructures memory and synthesizes tools.
  > Other layers write *to* memory but do not restructure it."
- Currently Deep Sleep is **broken at the import level** (we found
  this earlier in the session — `from deep_sleep.loop` doesn't
  resolve). Until it runs, no consolidation happens. Without
  consolidation, no procedural memory grows. Without procedural
  memory, nothing learns.
- This is a hard prerequisite for curiosity: a system that forgets
  its own questions overnight is not curious, just confused.

### 2.5 Metacognition: knowing what you don't know

Flavell (1979) introduced **metacognition** — knowledge about one's
own cognitive processes. Fleming & Dolan (2012) located the neural
basis in the rostrolateral prefrontal cortex: a region that monitors
the *confidence* of other cognitive processes, separate from the
processes themselves.

This second-order monitoring is what allows an organism to say "I
don't know" and *mean* it — not as a verbal hedge but as a
representational state with its own neural substrate. Without it,
knowing-that-one-doesn't-know is impossible, and so curiosity (which
requires that knowing) is impossible.

**Mapped to Omega:**
- The Meta layer in Omega's stack is named exactly for this. Its
  contract role: "short-range reasoning + immediate policy
  adjustment", "task interpretation, confidence scoring, failure
  diagnosis."
- Currently Meta exists but does not maintain a *gap inventory*. It
  reflects on cycle outcomes, not on absences. Adding a gap-detection
  pass to Meta is the metacognitive substrate curiosity needs.

### 2.6 Theory of mind and joint attention

Tomasello (2008, *Origins of Human Communication*) argued that human
communication is built on **shared intentionality** — two minds
attending to the same thing while *aware that they are both
attending to it*. Pre-linguistic infants demonstrate joint attention
through gaze-following and pointing well before they can speak. This
is bidirectional from the start.

Clark (1996, *Using Language*) frames conversation as **joint
action**: each utterance is a contribution to *common ground* — the
mutually-known store of facts and assumptions both speakers maintain.
Repair (re-asking, clarifying) is what keeps common ground accurate
when one party gets confused.

Frith & Frith (2003) reviewed the neural basis of mentalizing — the
medial prefrontal cortex and temporo-parietal junction. Modeling
*the other mind* is a continuous background process, not an
on-demand calculation.

**Mapped to Omega:**
- Omega has lobe 100 (`personal/user_profile/`) for storing facts
  about the user. It is currently empty. There is no theory of mind
  of Noah being built up from interaction.
- The contract has no notion of *common ground* per cycle — each
  conversation is treated as fresh inputs to a stateful memory, not
  as a dialogue with a maintained mutual context.
- Adding a `conversation_state` per session that tracks "what we've
  established together this session" would be the joint-action
  substrate. It would also dramatically improve response quality —
  the model could draw on what was established a few turns ago
  rather than re-grounding everything from cold memory each cycle.

### 2.7 Default mode network: internal mind-wandering

The brain's **default mode network** (DMN — Raichle et al., 2001)
activates when the brain is *not* doing a focused task — during
rest, daydreaming, mind-wandering, prospection, autobiographical
memory. Far from being noise, DMN activity is now understood as
critical for: (a) future planning, (b) self-reflection, (c) novel
association formation.

The DMN is what makes humans think about things even when nothing
is happening. It's the substrate for unprompted curiosity.

**Mapped to Omega:**
- Omega's autonomous loop is the DMN analog. It runs on a cadence
  (~60s per cycle), independent of user input. It's *supposed* to
  observe, reflect, infer, act, revise, evolve — internal cognition
  in the absence of external demand.
- Currently the autonomous loop runs but doesn't have *anything to
  do* in the absence of input. It cycles through phases producing
  empty observations. It doesn't notice gaps because nothing tells
  it to look for them.
- Adding gap-detection + question-generation to `auto.reflect` /
  `auto.infer` is what gives the autonomous loop a job during DMN
  time: notice what's unknown, plan how to find out.

### 2.8 Summary table — biology to Omega

| Biological principle | Omega substrate | Status |
|---|---|---|
| Information-gap theory (Loewenstein) | Memory contract `contradiction_state` field | Schema exists, runtime unused |
| Berlyne's collative variables | Memory's strength index + classifier | Partial; novelty signal missing |
| Dopamine RPE (Schultz) | `autonomic/dopamine/` | Write-only; no consumer |
| Compression progress (Schmidhuber) | Deep Sleep memory_management | Designed; broken at import |
| Active inference (Friston) | Brain loop `proposed_actions` + tool dispatch | Tool route exists; no user-ask route |
| Sleep consolidation (Stickgold) | Deep Sleep layer | Broken at import |
| Metacognition (Flavell, Fleming) | Meta layer | Exists; no gap inventory |
| Theory of mind (Tomasello) | `100_personal/user_profile/` | Empty |
| Common ground (Clark) | None | Missing entirely |
| Default mode network | Autonomous loop | Cycles run; no work to do |

The table reads as a roadmap.

---

## 3. AI state of the art and what's missing

### 3.1 Why current LLMs cannot be curious

Pretrained transformers are stateless function approximators. They
have no persistent state between calls; their "memory" of a
conversation is a sliding context window the developer maintains.
At the architectural level, there is no representation of:

- What the model doesn't know but wants to find out
- What was true last week but might not be true this week
- A question still open from a previous session
- The user's identity, accumulated over interactions

These cannot be added by prompt engineering. A prompt like "you are
a curious assistant" produces outputs that *describe* curiosity but
do not *maintain* the underlying state. The model's actual cognition
is no different than without the prompt.

### 3.2 Computational curiosity in reinforcement learning

The RL community has the most developed treatment of artificial
curiosity. The canonical work:

- **Pathak et al. (2017)**, *Curiosity-driven Exploration by
  Self-Supervised Prediction* (ICML): an agent rewards itself
  proportional to its model's prediction error on the next state.
  Encountering novel states (poorly predicted) gives intrinsic
  reward. Solves Mario without any extrinsic reward signal.
- **Burda et al. (2018)**, *Large-Scale Study of Curiosity-Driven
  Learning*: scaled the above to many environments; found the
  approach generalizes but suffers from the "noisy TV problem"
  (intrinsic reward maximizes on inherently-unpredictable inputs).
- **Random Network Distillation (Burda et al., 2018)**: novelty as
  prediction error against a randomly-initialized network — bypasses
  the noisy TV problem.
- **Oudeyer & Kaplan's** developmental robotics line: intrinsic
  motivation as *learning progress* — reward not raw novelty but the
  rate at which a region of state space becomes more predictable.

These approaches are real curiosity mechanisms but live entirely
inside a single agent's RL loop. They don't translate cleanly to
conversational AI because:
- They have no persistent symbolic memory of *what* was learned
- They cannot articulate questions in natural language
- They are confined to a single task / environment
- The "curiosity" is a scalar reward, not a structured open-question
  representation

### 3.3 Active learning — the closest classical-ML analog

Active learning (Settles 2009, *Active Learning Literature Survey*)
addresses: given a budget for labeling, which examples should the
learner request labels for? Strategies include:

- **Uncertainty sampling** — request labels for inputs the model is
  least confident about
- **Query by committee** — request labels where an ensemble of models
  disagrees most
- **Expected model change** — request labels expected to most update
  the model

This is pre-curiosity: it's selective question-asking with a clear
budget, but it doesn't generate novel questions — it picks from a
fixed pool. Still, the *spirit* is right: the system identifies its
own uncertainties and routes them outward for resolution.

### 3.4 Question generation as a sub-field

Du, Shao & Cardie (2017, *Learning to Ask*) trained
encoder-decoder models to generate questions from context. Krishna
et al. (2019) extended to information-maximizing visual questions.
These models can transform a piece of text into a question about it
— the technical primitive for curiosity's question-generation step.

But: nothing in this literature ties question generation to a
*persistent gap inventory* or to *outward routing*. The question
gets generated, returned, and forgotten.

### 3.5 World models and self-supervised structure-finding

LeCun (2022) proposed JEPA (Joint-Embedding Predictive Architecture)
as a path to autonomous machine intelligence: the system learns a
predictive world model from unlabeled experience, plans actions in
the latent space of that model, and minimizes its own prediction
error.

Ha & Schmidhuber (2018, *World Models*) demonstrated agents that
learn a compressed model of their environment and "dream" alternative
trajectories for planning.

These are upstream of curiosity: they build the *model* that can
have gaps. Without a generative model of the world, "what I don't
know" has nothing to be a gap *in*. The model is the substrate;
curiosity is what the substrate enables.

### 3.6 What everyone has missed

Across all this work, the same gap recurs: nobody ties together

(a) a **persistent symbolic representation** of the agent's
    knowledge state (and gaps in it),
(b) a **question-generation surface** that can articulate gaps in
    natural language,
(c) **outward routing** to a real human or real external source,
(d) **resolution tracking** that closes the loop when answers arrive,
(e) and a **drive signal** that maintains the system's want-to-know
    over long timescales.

The closest unified attempt is the developmental robotics line
(Oudeyer, Kaplan, et al.) — but it operates in sensorimotor space,
not language, and lacks (b) and (c).

**Omega's opportunity is exactly this synthesis.** The substrate
already supports (a) (memory contract). (b) is a small generative
model away (we have a 1.7B model that can do this if prompted right).
(c) needs the proactive output channel and the existing tool dispatch.
(d) needs a callback mechanism in memory writes. (e) needs the
already-existing dopamine signal to be consumed instead of just
written.

---

## 4. The Omega architecture mapping

### 4.1 What we already have

Omega's substrate uniquely supports two-way cognition because of
five existing pieces:

1. **Persistent memory cortex** — Dewey 3D structure with 10 lobes,
   schema-validated writes, atomic persistence, provenance tracking.
   This is the substrate for gap-accounting.
2. **Autonomic signal layer** — `dopamine`, `plasticity`,
   `strength`, `stress` modules that compute neuromodulator-analog
   signals per cycle. The drive infrastructure is wired.
3. **Five-layer reflective stack** — Autonomous (per-cycle execution
   + learning), Meta (short-range governance), Global (long-arc
   reflection), Deep Sleep (consolidation), Omega (rule evolution).
   Curiosity has natural homes in each layer.
4. **Brain loop with bounded postures** — Guardian-classified
   inputs route through ResponseClasses with explicit must / must_never
   bounds. The framework for *how Omega responds* is contractual,
   not just prompt-based.
5. **Tool dispatch with contract enforcement** — `web_fetch` is
   reachable from both the autonomous loop (Phase 3) and the brain
   loop (via reason's two-pass dispatch). The outward-routing
   primitive for non-human gaps already exists.

### 4.2 What's missing for two-way cognition

| Primitive | Where it should live | Build complexity |
|---|---|---|
| **Open-questions store** | New memory sub-structure under lobe 6 (journal) or its own lobe | Small — schema + write helpers |
| **Gap detector** | New step in `auto.reflect` that compares cycle outcomes to memory | Medium — logic to identify "this was needed but absent" |
| **Question generator** | New sub-phase of `auto.infer` that turns gaps into specific questions | Medium — model call with bounded prompt |
| **Question router** | Helper module deciding: ask user / web fetch / deep_sleep | Small — rule-based today, model-based later |
| **Proactive output channel** | New orchestrator method `push_to_user(message, source_question_id)` | Medium — needs a UI surface for proactive notifications |
| **Resolution callback** | Hook in `memory.api.write_memory` that checks open questions for closure | Small — list comprehension over open questions |
| **Curiosity drive consumption** | New step in Global that reads dopamine trend → adjusts question generation rate | Small — config-driven thresholds |
| **Theory-of-mind ledger** | Population of `100_personal/user_profile/` from each cycle | Medium — extraction model + write rules |
| **Common-ground tracker** | New per-session `conversation_state` object | Medium — structured state across cycles within a session |
| **First-contact handler** | Brain loop branch when user_profile is empty | Small — onboarding sequence |

### 4.3 The dependency graph

These primitives have ordering constraints:

```
        Open-questions store
                |
                v
        Gap detector ----------+
                |              |
                v              v
        Question generator     Theory-of-mind ledger
                |              |
                v              v
        Question router        First-contact handler
            /       \
           v         v
   Proactive       (existing)
   push channel    web_fetch / deep_sleep
                          |
                          v
                   Resolution callback
                          |
                          v
                Curiosity drive consumption
```

The **open-questions store** is the foundation — every other piece
references it. The **proactive push channel** is the second
foundation — without it, Omega can ask itself questions but cannot
*ask Noah*, and outward-facing cognition has no exit.

### 4.4 Critical dependency: Deep Sleep

Curiosity that doesn't compound is just confusion. The system needs
overnight consolidation to:

- Promote frequently-asked-but-never-answered questions to higher
  urgency
- Decay no-longer-relevant questions
- Find structural connections between separate questions
  ("these three things I've been wondering about are actually one
  question")
- Restructure the user-profile from accumulated cycle facts

Deep Sleep is currently broken at the import level (`from
deep_sleep.loop` doesn't resolve — see earlier session notes about
the path being `metacognition.deep_sleep.loop` but referenced as
the bare top-level). Repairing this is a hard prerequisite for the
curiosity work above to compound.

---

## 5. The Two-Way Cognition Framework

### 5.1 Definition

A system exhibits **two-way cognition** when it satisfies all of:

1. **State of unknown**: maintains a persistent, structured record
   of things it does not yet know (open questions).
2. **Gap detection**: automatically identifies new gaps from each
   cycle's outcomes — what was needed but absent.
3. **Question articulation**: can produce specific, answerable
   natural-language questions targeting its gaps.
4. **Outward routing**: can direct each question to the surface most
   likely to answer it (user dialogue, external lookup, internal
   reflection).
5. **Proactive expression**: can initiate communication — ask the
   user something without being prompted to.
6. **Resolution closure**: when an answer arrives, marks the
   question closed, updates confidence in dependent facts, and
   adjusts its drive level.
7. **Theory of the other**: maintains a model of what the other
   party knows, prefers, and has shared, used to choose what to ask
   and how.

### 5.2 The crawl: minimum viable curiosity

Three sequential builds, each independently shippable:

**Build 1: Persistent open-questions store**

- New schema `core/schema/open_question.py` — fields: `question_id`,
  `text`, `gap_type` (factual / preferential / temporal /
  procedural), `source_cycle_id`, `created_at`, `urgency`,
  `route_preference` (user / web / reflect), `last_asked_at`,
  `answer_status` (open / answered / decayed), `linked_memory_ids`
- New memory sub-structure: `memory/600_journal/open_questions/`
- Write API: `memory.api.write_open_question(question)` with
  schema validation, atomic write, audit trail
- Resolution API: `memory.api.resolve_open_question(qid,
  resolving_memory_id, evidence_summary)`
- Index: `open_questions_index.json` mapping urgency → question_ids
  for fast prioritization

**Build 2: Gap detection + question generation**

- New step in `auto.reflect`: `_detect_gaps(cycle_outcomes,
  recalled_memories) -> list[Gap]` — compares what the cycle needed
  vs what memory provided. Specific cases:
  - Empty recall on a factual question → `factual_gap`
  - Reason produced low confidence → `understanding_gap`
  - Verify flagged unsupported claim → `verification_gap`
  - User input contained a name not in user_profile → `identity_gap`
- New sub-phase `auto.infer`: `_generate_questions(gaps) ->
  list[OpenQuestion]` — uses tiny_cognitive with a locked prompt to
  turn each gap into a specific natural-language question.
  Routing decided by `gap_type`:
  - `factual_gap` → web_fetch route
  - `identity_gap` / `preferential_gap` → user route
  - `understanding_gap` / `verification_gap` → deep_sleep route
- Questions written to the open-questions store immediately.

**Build 3: Proactive output channel + first-contact handler**

- New orchestrator method `Orchestrator.maybe_initiate(user_channel)
  -> Optional[Response]`. Called periodically (e.g. once per
  autonomous cycle, with rate-limiting). Consults the open-questions
  store for `route_preference == user` items above an urgency
  threshold. If found, packages the question into a brain-loop cycle
  with `source_kind: omega_initiated` and pushes it to the user
  channel.
- Brain loop branch: when `100_personal/user_profile/` is empty and
  this is the first cycle of a session, run the **first-contact
  handler** — Omega introduces itself, asks who's at the keyboard,
  stores the answer with provenance `user:first_contact:<timestamp>`.
- UI surface: dev UI gets a "incoming from Omega" notification path
  in addition to the chat input. (Stretch: a system-tray notification
  for production.)

### 5.3 The walk: full curiosity (later)

Once the crawl works, the walk includes:

- **Theory-of-mind extraction** — every cycle's user input is parsed
  for facts about the user, written to `100_personal/user_profile/`
  with provenance and confidence
- **Common-ground tracker** — a `conversation_state` object
  maintained per session, holding what's been established mutually
  this session
- **Curiosity drive integration** — Global reads dopamine trend; if
  sustained low, increases question-generation rate and lowers the
  threshold for proactive pushes
- **Question synthesis in Deep Sleep** — overnight, related
  questions get merged, decayed questions get archived, novel
  cross-domain questions get generated from contradictions
- **Repair** — when Omega misunderstands, it asks for clarification
  (uses `proposed_actions: ask_user` in reason output) instead of
  fabricating

---

## 6. Doing this on tiny models

The orthodoxy is "scale fixes everything." Omega's bet is that
**state fixes more than scale**. Some specific reasons tiny models
are sufficient — and in some ways better — for two-way cognition:

### 6.1 Tiny models are honest about uncertainty

A 1.7B model trained with reasonable post-training will say "I don't
know" more readily than a frontier model. Its prior is closer to
"unknown" than the frontier model's prior of "confidently complete".
This is *useful for curiosity* — the system needs an honest
signal of "I don't know this" to detect gaps.

Frontier models actively erase uncertainty in their outputs through
RLHF. They're trained to sound confident. That's the opposite of
what curiosity requires.

### 6.2 Tiny models force memory to do the heavy lifting

A small model can't memorize the world. So everything beyond the
model's parametric knowledge has to be in memory. This forces the
substrate to be rich — exactly what the curiosity machinery needs.
A frontier model can fake having memory by retrieving from its own
weights; a tiny model cannot, which keeps the architecture honest.

### 6.3 Tiny models are fast enough for two-way cognition

Curiosity requires *frequent* small inferences: detect gap (1 call),
generate question (1 call), route (rule-based), push to user (1
call), receive answer (1 call), resolve (rule-based). A 1.7B model
on a Vulkan GPU does each in under a second. A frontier model would
take longer per call AND cost API tokens AND require a network.
Locality matters for proactive cognition — there's no "ask the
cloud what to ask the user."

### 6.4 The right division of labor

In Omega's architecture:

- **Tiny model (qwen3-1.7b on llama.cpp)** does cognition: question
  generation, gap interpretation, conversational repair, reflection
  synthesis. Speed matters more than depth.
- **Voice model (qwen3.5-2b on Lemonade)** renders surface
  language only — it's the vocal cord, not the decider.
- **The substrate** does state: memory, schema validation, drive
  signals, audit trails, lifecycle. The substrate IS the cognition,
  with the models as tools.

This is the contract's "model is inference tissue; the system is
cognition" applied to curiosity.

---

## 7. Risks and open questions

### 7.1 Annoyance budget

A truly curious system will ask questions. The user's tolerance for
this is finite. The crawl version must:

- Rate-limit proactive pushes (e.g. max 1 per N minutes)
- Allow user to silence specific questions ("don't ask me that
  again" → mark that question as user-rejected)
- Distinguish urgent gaps from idle ones

### 7.2 The noisy-TV problem

Computational curiosity in RL gets stuck on inherently-unpredictable
inputs (Burda et al.). For Omega, the equivalent is: random / noisy
user input could spawn unanswerable questions that flood the store.
Mitigation: question generation requires non-trivial confidence in
the gap being real (not just "I don't know what zxcvbnm means").

### 7.3 Identity verification chicken-and-egg

The original spark for this discussion: how does Omega know it's
Noah? Without authentication, the system can only infer identity
from accumulated interaction. The first-contact handler bootstraps
this — but for the first session, there's no prior. The deployment
model assumption (single-user-owned) carries the load until enough
interaction has accumulated to *learn* the user-fact.

This is the same chicken-and-egg humans solve over weeks of
cohabitation, and it's solvable here the same way: provisional
trust + accumulating evidence + ability to re-verify when needed.

### 7.4 Drive intensity calibration

Too high a curiosity drive = chatty, intrusive system. Too low =
inert. The dopamine integration needs careful calibration — likely
config-driven thresholds that the user can tune. Auto-tuning via
Omega's evolution layer is a long-arc possibility once the system
has enough self-modification authority.

### 7.5 Avoiding fake curiosity

Easy failure mode: the system generates questions because the
prompt told it to, not because there's a real gap. The schema must
require provenance — every open question carries the
`source_cycle_id` and `gap_type` that produced it. Questions
without traceable origin are flagged.

---

## 8. What I'd build first, if it were my call

In order:

1. **Repair Deep Sleep imports.** No further curiosity work
   compounds without consolidation. Smallest fix; biggest enabler.
2. **Open-questions store + schema.** A few hundred lines of code.
   No model changes. Pure substrate work. Becomes the foundation
   for everything after.
3. **First-contact handler in the brain loop.** Activates when
   `100_personal/user_profile/` is empty. Omega introduces itself,
   asks who you are, stores answers as `user_taught_fact` records
   with full provenance. Solves the "how does Omega know what it
   is / who you are" question by *teaching it through interaction*.
4. **Gap detector in auto.reflect.** Simple rules first: empty
   recall on factual question, low reason confidence, identity-gap
   from unknown name in user input. Writes Gaps to a new sub-store.
5. **Question generator in auto.infer.** Locked prompt against
   tiny_cognitive: "Given this gap, generate one specific
   answerable question. Output JSON with question text, route
   suggestion, urgency 0-1." Writes OpenQuestions.
6. **Proactive push channel.** Orchestrator gains
   `maybe_initiate()`. Dev UI gains an "Omega says:" notification
   strip.
7. **Resolution callback.** `memory.api.write_memory` checks open
   questions for ones the new memory might close; updates them.
8. **Dopamine consumption in Global.** Closes the curiosity loop.

That sequence takes the system from "reactive chatbot built on
contract" to "an actual two-way cognitive presence that maintains
its own questions, asks them outward, and learns from the answers."
At which point we are no longer building an LLM wrapper. We're
building the thing the contract has been describing since Day Zero.

---

## 9. References (selected, real, not synthetic)

- Berlyne, D. E. (1960). *Conflict, Arousal, and Curiosity.*
  McGraw-Hill.
- Burda, Y., Edwards, H., Pathak, D., Storkey, A., Darrell, T., &
  Efros, A. A. (2018). Large-scale study of curiosity-driven
  learning. *ICLR*.
- Clark, A. (2013). Whatever next? Predictive brains, situated
  agents, and the future of cognitive science. *Behavioral and
  Brain Sciences*, 36(3), 181–204.
- Clark, H. H. (1996). *Using Language.* Cambridge University Press.
- Dayan, P., & Niv, Y. (2008). Reinforcement learning: The good,
  the bad and the ugly. *Current Opinion in Neurobiology*, 18(2).
- Diekelmann, S., & Born, J. (2010). The memory function of sleep.
  *Nature Reviews Neuroscience*, 11(2), 114–126.
- Du, X., Shao, J., & Cardie, C. (2017). Learning to ask: Neural
  question generation for reading comprehension. *ACL*.
- Flavell, J. H. (1979). Metacognition and cognitive monitoring.
  *American Psychologist*, 34(10), 906–911.
- Fleming, S. M., & Dolan, R. J. (2012). The neural basis of
  metacognitive ability. *Philosophical Transactions of the Royal
  Society B*, 367(1594), 1338–1349.
- Friston, K. (2010). The free-energy principle: a unified brain
  theory? *Nature Reviews Neuroscience*, 11(2), 127–138.
- Frith, U., & Frith, C. D. (2003). Development and neurophysiology
  of mentalizing. *Phil. Trans. R. Soc. Lond. B*, 358(1431).
- Ha, D., & Schmidhuber, J. (2018). World models. *NeurIPS*.
- Kidd, C., & Hayden, B. Y. (2015). The psychology and neuroscience
  of curiosity. *Neuron*, 88(3), 449–460.
- Krishna, R., Bernstein, M., & Fei-Fei, L. (2019). Information
  maximizing visual question generation. *CVPR*.
- LeCun, Y. (2022). A path towards autonomous machine intelligence.
  *Open Review*.
- Loewenstein, G. (1994). The psychology of curiosity: A review and
  reinterpretation. *Psychological Bulletin*, 116(1), 75–98.
- Oudeyer, P.-Y., & Kaplan, F. (2007). What is intrinsic motivation?
  A typology of computational approaches. *Frontiers in
  Neurorobotics*, 1, 6.
- Pathak, D., Agrawal, P., Efros, A. A., & Darrell, T. (2017).
  Curiosity-driven exploration by self-supervised prediction.
  *ICML*.
- Raichle, M. E., MacLeod, A. M., Snyder, A. Z., Powers, W. J.,
  Gusnard, D. A., & Shulman, G. L. (2001). A default mode of brain
  function. *PNAS*, 98(2), 676–682.
- Schmidhuber, J. (1991). A possibility for implementing curiosity
  and boredom in model-building neural controllers. *Proc. of the
  international conference on simulation of adaptive behavior*.
- Schmidhuber, J. (2010). Formal theory of creativity, fun, and
  intrinsic motivation. *IEEE Trans. on Autonomous Mental
  Development*, 2(3), 230–247.
- Schultz, W., Dayan, P., & Montague, P. R. (1997). A neural
  substrate of prediction and reward. *Science*, 275(5306).
- Settles, B. (2009). Active learning literature survey. *University
  of Wisconsin–Madison Computer Sciences Technical Report 1648.*
- Stickgold, R. (2005). Sleep-dependent memory consolidation.
  *Nature*, 437(7063), 1272–1278.
- Tomasello, M. (2008). *Origins of Human Communication.* MIT Press.

---

## 10. Where I'd want your half of the research to go

The literature above is the AI/cognitive-science axis. The
complementary axes I'd value your perspective on:

- **Developmental psychology of inquiry** — when do children start
  asking "why?" questions, and what changes in their neural
  substrate around that age? (Searches: Chouinard's "Children's
  questions" 2007 monograph; Frazier, Gelman & Wellman work on
  question types.)
- **Joint attention in pre-linguistic infants** — pointing,
  gaze-following, pre-verbal back-and-forth. These are the
  proto-conversational acts; understanding them clarifies what
  Omega needs to *do* with you, not just *say*.
- **Pedagogy theory** — Vygotsky's zone of proximal development:
  learning happens in the gap between what you know and what you
  could know with help. Omega's "help" comes from you, the user.
  How does that relationship structurally work?
- **Personal experience** — how DO you teach a system you've built
  to know you? What would feel like genuine curiosity from Omega
  vs. simulated? You'd notice the difference faster than I would.
- **The constraints we should NOT lift** — what's in the contract
  that this whole curiosity machinery must NOT violate? Easy
  failure mode is "make it ask everything always" — what limits
  preserve the contract's spirit?

When we converge our two halves of the research, we should have
enough to design Build 1 (open-questions store) with the schema and
constraints actually correct. Then build it.

---

*End of working draft. This document lives at
`docs/research/two_way_cognition.md` for iteration.*
