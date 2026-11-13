"""Multi-angle stress corpus for the deterministic memory router.

Built 2026-06-19, scaled 2026-06-19, to stress the two deterministic routing
authorities from many real-world context angles and produce honest numbers:

  * ``subdomain_for(text, lobe)`` — the write + recall subdomain (Y) authority
    for static lobes, and the recall widen-seed for every lobe.
  * ``route_memory_tool(query)`` — the recall query → ``remember_*`` tool (lobe)
    router.

Ground-truth labels are SEMANTIC (where a human says the memory belongs), never
the grammar's own output — so when the grammar disagrees or returns ``None``
that is a real finding (coverage gap or misroute), not a tautology.

Production-routing reality this corpus respects:
  * Dynamic-Y lobes (2 knowledge, 8 coding) allocate Y from the knowledge /
    coding router at write time, so on the WRITE-NAT angle ``subdomain_for``
    returning ``None`` is the EXPECTED widen behaviour, not a failure. The
    WRITE-KW angle (subdomain name PRESENT) probes whether term-match can find
    the right Y when the word is there — there a ``None`` IS a coverage miss.
  * Several lobes carry explicit metadata at write (4 situational facet, 5 tools
    registration, 6 journal deepsleep, 7 vision modality, 9 temporal event_type,
    11 dev pipeline). For those, ``subdomain_for`` mainly backstops free-text
    writes and serves recall — so the ``symmetry_realistic`` angle is the
    load-bearing test for them, not write-side grammar coverage alone.

Angles
------
write_static      : free-text writes for the 9 static-Y lobes — subdomain_for is
                    (or backstops) the write authority. Decidability + accuracy.
write_dynamic_kw  : EXHAUSTIVE term-match probe — one case per knowledge (56) and
                    coding (50) subdomain with the name embedded (grammar best
                    case). Generated from SUBDOMAIN_MAP, perfectly labelled.
write_dynamic_nat : natural dynamic-lobe writes WITHOUT the literal domain word
                    (real world); grammar None expected, allocator owns Y.
recall_query      : operator recall questions → route_memory_tool lobe routing.
symmetry_realistic: (write_text, write_Y, differently-phrased recall_query)
                    triples — does subdomain_for(recall_query) reproduce write_Y?
cross_lobe        : sentences plausibly in 2+ lobes — diagnostic landing spread.
paraphrase        : paraphrase groups of one intent — intra-group Y agreement.
noise             : typo / casing / length perturbations of known-good cases.
negation          : negated / hypothetical keyword carriers — over-trigger check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from memory.dewey_map import SUBDOMAIN_MAP, UNSORTED_SUBDOMAIN_Y


@dataclass(frozen=True)
class StressCase:
    """One labelled stress example.

    ``expected_y`` is the SEMANTIC ground-truth subdomain (None == "grammar
    should widen / no single subdomain is correct"). For recall_query /
    cross_lobe, the expected/primary LOBE is carried in ``lobe``. ``recall_query``
    carries a differently-phrased recall (symmetry_realistic only). ``group``
    ties paraphrases together. ``plausible_lobes`` lists acceptable landing lobes
    for cross-lobe ambiguity cases.
    """

    angle: str
    text: str
    lobe: int
    expected_y: Optional[int] = None
    recall_query: Optional[str] = None
    group: Optional[str] = None
    plausible_lobes: tuple = ()
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Angle 1 — write_static: free-text writes for the 9 static-Y lobes.
# Labels are semantic. Natural no-trigger statements are labelled with their
# TRUE semantic Y (so a grammar miss registers as unsorted_miss) to expose the
# real coverage floor; expected_y is None only when no subdomain is warranted.
# ─────────────────────────────────────────────────────────────────────────────
WRITE_STATIC: List[StressCase] = [
    # ── Lobe 0 identity (0 constitution/operator-self, 1 personality/3rd-person,
    #     2 emergent_traits, 3 evolution[no grammar], 4 guardian/rules,
    #     5 self_inquiry/open-questions, 6 omega/2nd-person) ──
    StressCase("write_static", "I am Noah, the operator of this system", 0, 0),
    StressCase("write_static", "my name is Noah and I build sovereign AI", 0, 0),
    StressCase("write_static", "I work as a systems engineer", 0, 0),
    StressCase("write_static", "I live in the UK and run Omega locally", 0, 0),
    StressCase("write_static", "my role is to direct and build this system", 0, 0),
    StressCase("write_static", "you are Omega, a local sovereign intelligence", 0, 6),
    StressCase("write_static", "your purpose is to assist without the cloud", 0, 6),
    StressCase("write_static", "you exist to serve a single operator offline", 0, 6),
    StressCase("write_static", "you were created to run entirely on-device", 0, 6),
    StressCase("write_static", "one of my traits is stubborn persistence", 0, 2),
    StressCase("write_static", "the system shows curiosity as an emergent trait", 0, 2),
    StressCase("write_static", "a defining trait is caution under uncertainty", 0, 2),
    StressCase(
        "write_static",
        "your trait of directness shows often",
        0,
        2,
        note="'trait' (2) beats second-person 'your' (6) by order",
    ),
    StressCase("write_static", "an open question I keep returning to is what self means", 0, 5),
    StressCase("write_static", "open questions about my own continuity remain", 0, 5),
    StressCase(
        "write_static",
        "you must never delete operator memories",
        0,
        4,
        note="'never' (rule=4) beats second-person (omega=6) by order",
    ),
    StressCase("write_static", "this is a hard line: no telemetry leaves the device", 0, 4),
    StressCase("write_static", "privacy is non-negotiable", 0, 4),
    StressCase("write_static", "crossing into operator data is a boundary we never cross", 0, 4),
    StressCase(
        "write_static",
        "she is my sister and visits on weekends",
        0,
        1,
        note="possessive 'my' (→0) captures a third-person identity claim",
    ),
    StressCase("write_static", "he is the lead engineer on the project", 0, 1),
    StressCase("write_static", "they are the operator's long-time mentor", 0, 1),
    StressCase(
        "write_static",
        "the system evolved new caution after the rollback",
        0,
        3,
        note="evolution(0,3) has NO grammar trigger → widens → unsorted_miss",
    ),
    StressCase(
        "write_static",
        "my self-model has evolved over months",
        0,
        3,
        note="'my' captures evolution text into constitution → misroute",
    ),
    StressCase(
        "write_static",
        "Noah believes the device should outlive its vendor",
        0,
        0,
        note="proper noun, no pronoun → grammar widens; semantic=operator",
    ),
    # ── Lobe 1 personas (0 operator/1st-person, 1 person_stub/3rd-person) ──
    StressCase("write_static", "I prefer terse, direct answers", 1, 0),
    StressCase("write_static", "my coffee order is a flat white", 1, 0),
    StressCase("write_static", "I usually work late into the night", 1, 0),
    StressCase("write_static", "I dislike unnecessary meetings", 1, 0),
    StressCase("write_static", "remember that I take my tea black", 1, 0),
    StressCase("write_static", "she runs the lab next door", 1, 1),
    StressCase("write_static", "he leads the hardware team", 1, 1),
    StressCase("write_static", "they review the security patches each week", 1, 1),
    StressCase(
        "write_static",
        "he is my father and lives in Leeds",
        1,
        1,
        note="possessive 'my' wins → operator(0) misroute",
    ),
    StressCase(
        "write_static",
        "they mentor me on distributed systems",
        1,
        1,
        note="'me' (first-person) → operator(0) misroute",
    ),
    StressCase(
        "write_static", "she is my manager at the lab", 1, 1, note="'my' → operator(0) misroute"
    ),
    StressCase(
        "write_static",
        "Sarah runs the lab next door",
        1,
        1,
        note="proper noun only → grammar widens; semantic=person_stub",
    ),
    StressCase(
        "write_static",
        "Dr. Lee teaches the evening seminar",
        1,
        1,
        note="proper noun only → widens",
    ),
    # ── Lobe 3 reasoning (0 goals,1 decisions,2 constraints,3 heuristics,4 alignment) ──
    StressCase("write_static", "our goal is to ship deterministic routing", 3, 0),
    StressCase("write_static", "the objective this quarter is full offline support", 3, 0),
    StressCase("write_static", "we aim to make recall perfectly symmetric", 3, 0),
    StressCase("write_static", "the target is zero probabilistic routing", 3, 0),
    StressCase("write_static", "we decided to retire the SetFit classifier", 3, 1),
    StressCase("write_static", "I chose grammar routing over a learned model", 3, 1),
    StressCase("write_static", "the choice was to stay fully offline", 3, 1),
    StressCase("write_static", "we decided against shipping a model", 3, 1),
    StressCase("write_static", "a hard constraint is no probabilistic lobe selection", 3, 2),
    StressCase("write_static", "the model must not write to memory directly", 3, 2),
    StressCase("write_static", "we cannot exceed eight gigabytes of RAM", 3, 2),
    StressCase("write_static", "writing to disk during inference is forbidden", 3, 2),
    StressCase("write_static", "a useful heuristic: undecidable text goes to _unsorted", 3, 3),
    StressCase("write_static", "rule of thumb — prefer determinism over recall breadth", 3, 3),
    StressCase("write_static", "the guiding principle is honesty over convenient defaults", 3, 3),
    StressCase("write_static", "our alignment is toward operator sovereignty", 3, 4),
    StressCase("write_static", "the core values are privacy and honesty", 3, 4),
    StressCase("write_static", "stay aligned to offline-first at all costs", 3, 4),
    StressCase(
        "write_static",
        "the goal is to decide which subdomains to add",
        3,
        0,
        note="'decide' (decisions=1) beats 'goal' (goals=0); semantic=goals",
    ),
    StressCase(
        "write_static",
        "we weighed latency against privacy and kept it local",
        3,
        1,
        note="decision, no trigger word → unsorted_miss",
    ),
    StressCase(
        "write_static",
        "keeping routing deterministic matters more than breadth",
        3,
        4,
        note="alignment, no trigger word → unsorted_miss",
    ),
    StressCase(
        "write_static",
        "we reasoned the allocator should own dynamic Y",
        3,
        None,
        note="reasoning statement, no single subdomain → widen",
    ),
    # ── Lobe 4 situational (6 outcome,7 sentiment,0 who,1 what,2 when,3 where,4 why,5 how) ──
    StressCase("write_static", "the outcome was a clean boot with no errors", 4, 6),
    StressCase("write_static", "the result of the deploy was a rollback", 4, 6),
    StressCase("write_static", "the consequence was three hours of lost work", 4, 6),
    StressCase("write_static", "I felt frustrated debugging the gates last night", 4, 7),
    StressCase("write_static", "the mood in the session was cautiously optimistic", 4, 7),
    StressCase("write_static", "there was real anxiety before the launch", 4, 7),
    StressCase("write_static", "who approved the schema change", 4, 0),
    StressCase("write_static", "who was on call during the outage", 4, 0),
    StressCase("write_static", "what broke during consolidation", 4, 1),
    StressCase("write_static", "when did the runtime go down", 4, 2),
    StressCase("write_static", "where did the regression first appear", 4, 3),
    StressCase("write_static", "why did the embed worker hang", 4, 4),
    StressCase("write_static", "how did we recover the corrupted index", 4, 5),
    StressCase(
        "write_static",
        "what was the outcome of the migration",
        4,
        6,
        note="'outcome' (6) beats 'what' (1) by order",
    ),
    StressCase(
        "write_static",
        "how did you feel about the rollback",
        4,
        7,
        note="'feel' (sentiment=7) beats 'how' (5) by order",
    ),
    StressCase(
        "write_static",
        "the build finally succeeded after the gate fix",
        4,
        6,
        note="natural event, no facet word → unsorted_miss; semantic=outcome",
    ),
    StressCase(
        "write_static",
        "everyone was relieved once it booted clean",
        4,
        7,
        note="'relieved' is not a sentiment trigger → unsorted_miss",
    ),
    StressCase(
        "write_static",
        "the deploy failed at 3am and we rolled back",
        4,
        6,
        note="no facet word → unsorted_miss; semantic=outcome",
    ),
    # ── Lobe 5 tools (0 cognitive, 1 dev) ──
    StressCase("write_static", "the web_fetch tool retrieves a URL for the operator", 5, 0),
    StressCase("write_static", "the search tool queries the local index", 5, 0),
    StressCase("write_static", "the browse tool fetches and summarizes a page", 5, 0),
    StressCase("write_static", "this cognitive tool answers operator questions", 5, 0),
    StressCase("write_static", "the git commit helper is a dev-pipeline tool", 5, 1),
    StressCase("write_static", "the deploy tool pushes the built artifact", 5, 1),
    StressCase("write_static", "the compile step is part of the dev pipeline", 5, 1),
    StressCase("write_static", "the cicd runner triggers on each merge", 5, 1),
    StressCase(
        "write_static",
        "the helper that wraps the model endpoint",
        5,
        0,
        note="cognitive tool, no trigger word → unsorted_miss",
    ),
    # ── Lobe 6 journal (always deepsleep=0) ──
    StressCase("write_static", "deepsleep consolidated 14 episodes into 3 milestones", 6, 0),
    StressCase("write_static", "tonight's reflection noted recurring gate failures", 6, 0),
    StressCase("write_static", "the consolidation cascade ran to completion", 6, 0),
    StressCase("write_static", "journal entry: three regressions surfaced this week", 6, 0),
    StressCase("write_static", "nightly maintenance summary written at epoch close", 6, 0),
    # ── Lobe 7 vision (1 face,2 scene,3 patterns,0 images) ──
    StressCase("write_static", "I recognize this face from the camera feed", 7, 1),
    StressCase("write_static", "stored a facial embedding for the operator", 7, 1),
    StressCase("write_static", "match this person's face against the registry", 7, 1),
    StressCase("write_static", "the scene shows a desk with two monitors", 7, 2),
    StressCase("write_static", "describe the scene captured in this frame", 7, 2),
    StressCase("write_static", "detected a recurring visual pattern in the frames", 7, 3),
    StressCase("write_static", "the patterns in the lighting repeat each loop", 7, 3),
    StressCase("write_static", "saved a photo of the whiteboard", 7, 0),
    StressCase("write_static", "the image contains a circuit diagram", 7, 0),
    StressCase("write_static", "store this photograph of the setup", 7, 0),
    StressCase(
        "write_static",
        "a photo showing a face at the door",
        7,
        1,
        note="'face' (1) beats 'photo' (0) by order; semantic=face",
    ),
    StressCase(
        "write_static",
        "the scene in this image is a server room",
        7,
        2,
        note="'scene' (2) beats 'image' (0) by order",
    ),
    StressCase(
        "write_static",
        "what is on the screen right now",
        7,
        0,
        note="vision, no modality word → unsorted_miss",
    ),
    # ── Lobe 9 temporal (0 sessions,1 events,2 milestones,3 history) ──
    StressCase("write_static", "the session started at noon", 9, 0),
    StressCase("write_static", "this session covered deterministic routing", 9, 0),
    StressCase("write_static", "we hit the milestone of 1500 passing tests", 9, 2),
    StressCase("write_static", "the achievement of full offline mode landed today", 9, 2),
    StressCase("write_static", "the timeline shows three regressions this month", 9, 3),
    StressCase("write_static", "historical context: the project began in March", 9, 3),
    StressCase("write_static", "an event occurred: the runtime crashed at 3am", 9, 1),
    StressCase("write_static", "the outage occurred overnight", 9, 1),
    StressCase(
        "write_static",
        "the event happened during this session",
        9,
        0,
        note="'session' (0) beats 'event/happened' (1) by order",
    ),
    StressCase(
        "write_static",
        "a milestone event closed the quarter",
        9,
        2,
        note="'milestone' (2) beats 'event' (1) by order",
    ),
    StressCase(
        "write_static",
        "we shipped the first beta on Tuesday",
        9,
        2,
        note="milestone, no trigger word → unsorted_miss",
    ),
    # ── Lobe 11 dev (0 artifacts,1 progress,2 context,3 vision) ──
    StressCase("write_static", "the build produced a signed APK artifact", 11, 0),
    StressCase("write_static", "the binary output is in the dist folder", 11, 0),
    StressCase("write_static", "the artifact was uploaded to the release bucket", 11, 0),
    StressCase("write_static", "progress update: phase 7 harness is green", 11, 1),
    StressCase("write_static", "status: the migration is halfway done", 11, 1),
    StressCase("write_static", "an update on the loader landed this morning", 11, 1),
    StressCase("write_static", "the roadmap for the distro is the next priority", 11, 3),
    StressCase("write_static", "the plan for v1.1 adds Linux support", 11, 3),
    StressCase("write_static", "dev context: the gateway listens on 11434", 11, 2),
    StressCase("write_static", "background: the runtime is a separate process", 11, 2),
    StressCase("write_static", "setup notes for the dev environment", 11, 2),
    StressCase(
        "write_static",
        "the roadmap artifact was checked in",
        11,
        0,
        note="'artifact' (0) beats 'roadmap' (3); semantic=vision → misroute",
    ),
    StressCase(
        "write_static",
        "we still need to wire the loader",
        11,
        1,
        note="progress, no trigger word → unsorted_miss",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Angle 2a — write_dynamic_kw: EXHAUSTIVE term-match coverage probe. One case
# per knowledge (lobe 2) and coding (lobe 8) subdomain, with the subdomain's
# human-readable name embedded so the full-name keyword is present. Generated
# from SUBDOMAIN_MAP → labels are correct by construction. A miss = a genuine
# term-match coverage gap; a misroute = a genuine keyword collision (e.g. a bare
# name shadowed by a longer composite subdomain that shares a part-word).
# ─────────────────────────────────────────────────────────────────────────────
_KW_TEMPLATES = (
    "this note records key facts about {name}",
    "a reference entry on {name} and its core ideas",
    "summary of what we studied regarding {name}",
    "the topic captured here is {name} in depth",
)


def _gen_dynamic_kw() -> List[StressCase]:
    cases: List[StressCase] = []
    i = 0
    for (x, y), raw in sorted(SUBDOMAIN_MAP.items()):
        if x not in (2, 8):
            continue
        if y == UNSORTED_SUBDOMAIN_Y or raw == "_unsorted":
            continue
        readable = raw.replace("_", " ")
        text = _KW_TEMPLATES[i % len(_KW_TEMPLATES)].format(name=readable)
        i += 1
        cases.append(
            StressCase("write_dynamic_kw", text, x, y, note=f"term-match probe ({x},{y}) {raw}")
        )
    return cases


WRITE_DYNAMIC_KW: List[StressCase] = _gen_dynamic_kw()


# ─────────────────────────────────────────────────────────────────────────────
# Angle 2b — write_dynamic_nat: natural dynamic-lobe text WITHOUT the literal
# domain word. Grammar None is EXPECTED (allocator owns Y). expected_y = the
# domain the text is really about (so a grammar hit shows as a bonus).
# ─────────────────────────────────────────────────────────────────────────────
WRITE_DYNAMIC_NAT: List[StressCase] = [
    StressCase(
        "write_dynamic_nat", "black holes warp spacetime so strongly light cannot escape", 2, 0
    ),
    StressCase("write_dynamic_nat", "the planet completes one orbit every 88 days", 2, 0),
    StressCase("write_dynamic_nat", "entanglement links two particles across any distance", 2, 2),
    StressCase("write_dynamic_nat", "superposition lets a qubit hold both states at once", 2, 2),
    StressCase("write_dynamic_nat", "CRISPR edits DNA by cutting at a guide sequence", 2, 5),
    StressCase("write_dynamic_nat", "recessive alleles only show when both copies match", 2, 5),
    StressCase(
        "write_dynamic_nat", "the hippocampus consolidates short-term into long-term memory", 2, 6
    ),
    StressCase("write_dynamic_nat", "dopamine modulates reward-driven learning", 2, 6),
    StressCase(
        "write_dynamic_nat", "mitochondria produce ATP through the electron transport chain", 2, 4
    ),
    StressCase("write_dynamic_nat", "ribosomes translate messenger RNA into proteins", 2, 4),
    StressCase("write_dynamic_nat", "supply and demand set the clearing price", 2, 35),
    StressCase("write_dynamic_nat", "inflation erodes the purchasing power of savings", 2, 35),
    StressCase("write_dynamic_nat", "a one-way hash cannot be reversed to its input", 2, 26),
    StressCase(
        "write_dynamic_nat", "public keys encrypt what only the private key can open", 2, 26
    ),
    StressCase(
        "write_dynamic_nat", "the categorical imperative tests a maxim by universality", 2, 38
    ),
    StressCase("write_dynamic_nat", "tectonic plates drift on the asthenosphere", 2, 8),
    StressCase("write_dynamic_nat", "the GIL serializes bytecode execution across threads", 8, 1),
    StressCase("write_dynamic_nat", "await yields control back to the event loop", 8, 1),
    StressCase("write_dynamic_nat", "a hash map gives average O(1) lookups", 8, 29),
    StressCase("write_dynamic_nat", "a balanced tree keeps lookups logarithmic", 8, 29),
    StressCase("write_dynamic_nat", "the LRU policy drops the oldest unused entry", 8, 37),
    StressCase("write_dynamic_nat", "TLS negotiates a shared secret over an open channel", 8, 34),
    StressCase("write_dynamic_nat", "a mutex prevents two threads touching shared state", 8, 32),
    StressCase("write_dynamic_nat", "the borrow checker rejects two mutable references", 8, 12),
    StressCase("write_dynamic_nat", "goroutines multiplex onto OS threads", 8, 16),
    StressCase("write_dynamic_nat", "a prepared statement avoids injection in queries", 8, 22),
    StressCase("write_dynamic_nat", "the container image layers are cached per step", 8, 40),
    StressCase("write_dynamic_nat", "a rebase rewrites commits onto a new base", 8, 49),
]


# ─────────────────────────────────────────────────────────────────────────────
# Angle 3 — recall_query: operator recall questions → route_memory_tool lobe.
# The expected LOBE is carried in ``lobe``.
# ─────────────────────────────────────────────────────────────────────────────
RECALL_QUERY: List[StressCase] = [
    # ── queries the router HAS an intent for (should hit) ──
    StressCase("recall_query", "who am I to you?", 0),
    StressCase("recall_query", "what are my traits", 0),
    StressCase("recall_query", "my traits — what stands out", 0),
    StressCase("recall_query", "what do you remember about me", 1),
    StressCase("recall_query", "why did we decide to drop the classifier", 3),
    StressCase("recall_query", "why did we decide on grammar routing", 3),
    StressCase("recall_query", "what do you remember about this project", 8),
    StressCase("recall_query", "what do you remember about this codebase", 8),
    StressCase("recall_query", "what do you remember about this python project", 8),
    StressCase(
        "recall_query",
        "what do you remember about this project dev work",
        11,
        note="dev marker → remember_dev (11)",
    ),
    StressCase(
        "recall_query", "what do you remember about the implementation in this codebase", 11
    ),
    StressCase("recall_query", "how do we do a clean boot", 5),
    StressCase("recall_query", "how to enroll a new face", 5),
    StressCase("recall_query", "how to run the stress harness", 5),
    StressCase("recall_query", "where were we last session", 9),
    StressCase("recall_query", "what happened last time we deployed", 9),
    StressCase("recall_query", "last time we touched the gate, what changed", 9),
    StressCase("recall_query", "who is this face on the camera", 7),
    StressCase("recall_query", "who is this person in the frame", 7),
    StressCase("recall_query", "recognize this face", 7),
    StressCase("recall_query", "what is my mastery in python", 2),
    StressCase("recall_query", "what is my progress in rust", 2),
    StressCase("recall_query", "what is my trajectory", 9),
    StressCase("recall_query", "what is omega's trajectory", 9),
    StressCase("recall_query", "what do you know about black holes", 2),
    StressCase("recall_query", "what do you know about the hippocampus", 2),
    StressCase("recall_query", "what do you know about TLS handshakes", 2),
    # ── diagnostic MISROUTE: 'open questions' → reasoning, but they live at (0,5) ──
    StressCase(
        "recall_query",
        "what are my open questions",
        0,
        note="MISROUTE: router sends 'open questions'→reasoning(3); home is identity(0,5)",
    ),
    # ── diagnostic MISSES: no router intent → fall through to knowledge(2) ──
    StressCase(
        "recall_query",
        "what goals did we set",
        3,
        note="MISS: no goals recall intent → knowledge(2)",
    ),
    StressCase(
        "recall_query",
        "what is our objective for offline mode",
        3,
        note="MISS: no goals/objective recall intent",
    ),
    StressCase(
        "recall_query",
        "what constraints did we agree on",
        3,
        note="MISS: no constraints recall intent",
    ),
    StressCase("recall_query", "what tools can you use", 5, note="MISS: no tools recall intent"),
    StressCase(
        "recall_query",
        "which cognitive tools are registered",
        5,
        note="MISS: no tools recall intent",
    ),
    StressCase(
        "recall_query",
        "how did I feel about the launch",
        4,
        note="MISS: 'how' but no sentiment recall intent → knowledge(2)",
    ),
    StressCase(
        "recall_query",
        "what was the outcome of the migration",
        4,
        note="MISS: no situational recall intent",
    ),
    StressCase(
        "recall_query", "what milestones have we hit", 9, note="MISS: no milestones recall intent"
    ),
    StressCase(
        "recall_query", "what events happened overnight", 9, note="MISS: no events recall intent"
    ),
    StressCase(
        "recall_query",
        "what artifacts did the build produce",
        11,
        note="MISS: no dev-artifacts recall intent",
    ),
    StressCase(
        "recall_query",
        "what's the roadmap for the distro",
        11,
        note="MISS: no dev-vision recall intent",
    ),
    StressCase(
        "recall_query",
        "what scenes have you captured",
        7,
        note="MISS: no scene recall intent → knowledge(2)",
    ),
    StressCase(
        "recall_query",
        "what did deepsleep consolidate last night",
        6,
        note="MISS: no journal recall intent",
    ),
    StressCase(
        "recall_query",
        "who do I know named Sarah",
        1,
        note="MISS: no person-by-name recall intent → knowledge(2)",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Angle 4 — symmetry_realistic: the load-bearing test. Memory WRITTEN under one
# phrasing (text, lobe, expected_y) and RECALLED under a different natural
# phrasing (recall_query). Check subdomain_for(recall_query)==expected_y.
# ─────────────────────────────────────────────────────────────────────────────
SYMMETRY_REALISTIC: List[StressCase] = [
    StressCase(
        "symmetry_realistic",
        "you must never expose telemetry off-device",
        0,
        4,
        recall_query="what is the rule about telemetry",
    ),
    StressCase(
        "symmetry_realistic", "I am Noah, the operator", 0, 0, recall_query="who is the operator"
    ),
    StressCase(
        "symmetry_realistic",
        "one of my traits is persistence",
        0,
        2,
        recall_query="what personality characteristics do I have",
        note="'traits' write vs 'characteristics' recall — synonym gap",
    ),
    StressCase(
        "symmetry_realistic",
        "an open question is what continuity means",
        0,
        5,
        recall_query="what open questions am I holding",
    ),
    StressCase(
        "symmetry_realistic",
        "you exist to run offline for one operator",
        0,
        6,
        recall_query="what is your purpose",
    ),
    StressCase(
        "symmetry_realistic",
        "we decided to retire the classifier",
        3,
        1,
        recall_query="what decision did we make about the classifier",
    ),
    StressCase(
        "symmetry_realistic",
        "our goal is full offline operation",
        3,
        0,
        recall_query="what is our objective for offline mode",
    ),
    StressCase(
        "symmetry_realistic",
        "a constraint: no probabilistic lobe selection",
        3,
        2,
        recall_query="what are the limits on lobe selection",
    ),
    StressCase(
        "symmetry_realistic",
        "a heuristic: undecidable goes to _unsorted",
        3,
        3,
        recall_query="what is our rule of thumb for undecidable text",
    ),
    StressCase(
        "symmetry_realistic",
        "our alignment is operator sovereignty",
        3,
        4,
        recall_query="what values guide the system",
    ),
    StressCase(
        "symmetry_realistic",
        "I felt frustrated debugging the gate",
        4,
        7,
        recall_query="what was my emotion during the gate work",
    ),
    StressCase(
        "symmetry_realistic",
        "the deploy outcome was a rollback",
        4,
        6,
        recall_query="what was the result of the deploy",
    ),
    StressCase(
        "symmetry_realistic",
        "who approved the schema change",
        4,
        0,
        recall_query="who signed off on the schema",
    ),
    StressCase(
        "symmetry_realistic",
        "the web_fetch tool retrieves a URL",
        5,
        0,
        recall_query="which cognitive tool fetches a web page",
    ),
    StressCase(
        "symmetry_realistic",
        "the deploy tool ships the artifact",
        5,
        1,
        recall_query="what dev tool handles deployment",
    ),
    StressCase(
        "symmetry_realistic",
        "stored a facial embedding for the operator",
        7,
        1,
        recall_query="show me the saved face data",
    ),
    StressCase(
        "symmetry_realistic",
        "saved a photo of the whiteboard",
        7,
        0,
        recall_query="find the image of the whiteboard",
    ),
    StressCase(
        "symmetry_realistic",
        "the scene shows a server room",
        7,
        2,
        recall_query="describe the scene we captured",
    ),
    StressCase(
        "symmetry_realistic",
        "we hit the milestone of 1500 tests",
        9,
        2,
        recall_query="what achievement did we reach on tests",
        note="'milestone' write vs 'achievement' recall — both trigger milestones",
    ),
    StressCase(
        "symmetry_realistic",
        "the session started at noon",
        9,
        0,
        recall_query="when did this session begin",
    ),
    StressCase(
        "symmetry_realistic",
        "the runtime crash happened at 3am",
        9,
        1,
        recall_query="what event occurred overnight",
    ),
    StressCase(
        "symmetry_realistic",
        "the timeline shows three regressions",
        9,
        3,
        recall_query="what is the history of regressions",
    ),
    StressCase(
        "symmetry_realistic",
        "the build produced a signed APK",
        11,
        0,
        recall_query="where is the build artifact",
    ),
    StressCase(
        "symmetry_realistic",
        "progress update: phase 7 is green",
        11,
        1,
        recall_query="what is the status of phase 7",
    ),
    StressCase(
        "symmetry_realistic",
        "the roadmap adds Linux in v1.1",
        11,
        3,
        recall_query="what is the plan for v1.1",
    ),
    StressCase(
        "symmetry_realistic",
        "dev context: the gateway is on 11434",
        11,
        2,
        recall_query="what is the background on the gateway port",
    ),
    StressCase(
        "symmetry_realistic",
        "she is my sister",
        0,
        1,
        recall_query="who is the operator's sister",
        note="write captured by 'my'→constitution; recall is third-person",
    ),
    StressCase(
        "symmetry_realistic",
        "the consequence was lost work",
        4,
        6,
        recall_query="what was the outcome of the failure",
    ),
    StressCase(
        "symmetry_realistic",
        "the search tool queries the index",
        5,
        0,
        recall_query="what tool searches the local index",
    ),
    StressCase(
        "symmetry_realistic",
        "I chose to stay offline",
        3,
        1,
        recall_query="what choice did I make about connectivity",
    ),
    StressCase(
        "symmetry_realistic",
        "the constraint is one operator only",
        3,
        2,
        recall_query="what restriction applies to operators",
    ),
    StressCase(
        "symmetry_realistic",
        "the achievement of offline mode landed",
        9,
        2,
        recall_query="what milestone did we hit",
    ),
    StressCase(
        "symmetry_realistic",
        "why did the embed worker hang",
        4,
        4,
        recall_query="why did the worker stall",
    ),
    StressCase(
        "symmetry_realistic",
        "detected a recurring visual pattern",
        7,
        3,
        recall_query="what patterns did we see in the frames",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Angle 5 — cross_lobe: sentences plausibly in 2+ lobes. ``lobe`` is the primary
# semantic guess; ``plausible_lobes`` is the acceptable set. We record where
# route_memory_tool lands and whether it is plausible.
# ─────────────────────────────────────────────────────────────────────────────
CROSS_LOBE: List[StressCase] = [
    StressCase("cross_lobe", "I decided to study quantum physics", 0, plausible_lobes=(0, 2, 3)),
    StressCase("cross_lobe", "my goal is to learn cryptography", 3, plausible_lobes=(0, 2, 3)),
    StressCase(
        "cross_lobe", "we built a tool to deploy the python service", 5, plausible_lobes=(5, 8, 11)
    ),
    StressCase(
        "cross_lobe", "the session where we decided the schema", 9, plausible_lobes=(3, 4, 9)
    ),
    StressCase("cross_lobe", "how do we debug the async worker", 5, plausible_lobes=(5, 8, 10)),
    StressCase("cross_lobe", "I feel the build pipeline is fragile", 4, plausible_lobes=(4, 5, 11)),
    StressCase(
        "cross_lobe", "the face recognition model needs retraining", 7, plausible_lobes=(2, 7, 8)
    ),
    StressCase(
        "cross_lobe", "our principle is to keep history immutable", 3, plausible_lobes=(3, 9)
    ),
    StressCase(
        "cross_lobe", "the operator decided to log every session", 1, plausible_lobes=(1, 3, 9)
    ),
    StressCase(
        "cross_lobe", "why did the deploy tool fail last night", 4, plausible_lobes=(4, 5, 9)
    ),
    StressCase(
        "cross_lobe", "remember how I felt when the demo crashed", 4, plausible_lobes=(0, 4, 9)
    ),
    StressCase(
        "cross_lobe", "the milestone was deciding to drop the classifier", 9, plausible_lobes=(3, 9)
    ),
    StressCase(
        "cross_lobe", "what tools do we use to test rust code", 5, plausible_lobes=(5, 8, 10)
    ),
    StressCase("cross_lobe", "my values shape which goals I set", 3, plausible_lobes=(0, 3)),
    StressCase(
        "cross_lobe", "the scene from the camera showed a new face", 7, plausible_lobes=(7,)
    ),
    StressCase(
        "cross_lobe", "we recorded the workflow for code review", 10, plausible_lobes=(5, 8, 10)
    ),
    StressCase(
        "cross_lobe", "the artifact from the build broke in testing", 11, plausible_lobes=(8, 11)
    ),
    StressCase(
        "cross_lobe", "I want to understand how neurons encode memory", 2, plausible_lobes=(0, 2)
    ),
    StressCase(
        "cross_lobe", "the constraint forced us to refactor the cache", 3, plausible_lobes=(3, 8)
    ),
    StressCase("cross_lobe", "who decided the alignment of the project", 4, plausible_lobes=(3, 4)),
    StressCase("cross_lobe", "the history of our decisions on routing", 9, plausible_lobes=(3, 9)),
    StressCase("cross_lobe", "you should never deploy without tests", 0, plausible_lobes=(0, 3, 5)),
    StressCase("cross_lobe", "a photo of the team after the milestone", 7, plausible_lobes=(7, 9)),
    StressCase("cross_lobe", "how to write async python correctly", 5, plausible_lobes=(5, 8, 10)),
    StressCase(
        "cross_lobe", "the goal of this session was to fix recall", 9, plausible_lobes=(3, 9)
    ),
    StressCase(
        "cross_lobe", "my trait of caution shaped the rollback plan", 0, plausible_lobes=(0, 3, 11)
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Angle 6 — paraphrase: groups of one intent. All members share (lobe, y); the
# report checks every paraphrase lands on the SAME write coordinate.
# ─────────────────────────────────────────────────────────────────────────────
PARAPHRASE: List[StressCase] = [
    # g_goal — reasoning goals (3,0)
    StressCase("paraphrase", "our goal is to ship deterministic routing", 3, 0, group="g_goal"),
    StressCase("paraphrase", "the objective is deterministic routing", 3, 0, group="g_goal"),
    StressCase("paraphrase", "we aim to make routing deterministic", 3, 0, group="g_goal"),
    StressCase("paraphrase", "the target is deterministic routing", 3, 0, group="g_goal"),
    StressCase(
        "paraphrase",
        "what we want is fully deterministic routing",
        3,
        0,
        group="g_goal",
        note="no goal-word → likely widen",
    ),
    # g_rule — identity guardian (0,4)
    StressCase("paraphrase", "you must never delete operator memories", 0, 4, group="g_rule"),
    StressCase(
        "paraphrase",
        "deleting operator memories is forbidden",
        0,
        4,
        group="g_rule",
        note="'forbidden' is a lobe-3 word, not identity grammar → miss",
    ),
    StressCase(
        "paraphrase", "there is a hard line against deleting memories", 0, 4, group="g_rule"
    ),
    StressCase("paraphrase", "it is a non-negotiable rule to keep memories", 0, 4, group="g_rule"),
    # g_feel — situational sentiment (4,7)
    StressCase("paraphrase", "I felt frustrated during the debug session", 4, 7, group="g_feel"),
    StressCase("paraphrase", "my mood was frustrated while debugging", 4, 7, group="g_feel"),
    StressCase("paraphrase", "the emotion was frustration during the debug", 4, 7, group="g_feel"),
    StressCase(
        "paraphrase",
        "debugging left me annoyed and drained",
        4,
        7,
        group="g_feel",
        note="no sentiment trigger word → likely widen",
    ),
    # g_face — vision face (7,1)
    StressCase("paraphrase", "store this face embedding", 7, 1, group="g_face"),
    StressCase("paraphrase", "save the facial features from the camera", 7, 1, group="g_face"),
    StressCase("paraphrase", "remember this person's face", 7, 1, group="g_face"),
    StressCase(
        "paraphrase",
        "log the visage from the webcam",
        7,
        1,
        group="g_face",
        note="'visage' synonym not in grammar → likely widen",
    ),
    # g_decision — reasoning decisions (3,1)
    StressCase("paraphrase", "we decided to retire the classifier", 3, 1, group="g_decision"),
    StressCase("paraphrase", "we chose to retire the classifier", 3, 1, group="g_decision"),
    StressCase("paraphrase", "the decision was to retire the classifier", 3, 1, group="g_decision"),
    StressCase(
        "paraphrase",
        "we settled on retiring the classifier",
        3,
        1,
        group="g_decision",
        note="'settled on' synonym not in grammar → likely widen",
    ),
    # g_constraint — reasoning constraints (3,2)
    StressCase(
        "paraphrase", "a constraint is no probabilistic routing", 3, 2, group="g_constraint"
    ),
    StressCase("paraphrase", "we must not use probabilistic routing", 3, 2, group="g_constraint"),
    StressCase("paraphrase", "probabilistic routing is forbidden", 3, 2, group="g_constraint"),
    StressCase("paraphrase", "we cannot use probabilistic routing", 3, 2, group="g_constraint"),
    StressCase(
        "paraphrase",
        "there is a hard limit against probabilistic routing",
        3,
        2,
        group="g_constraint",
    ),
    # g_outcome — situational outcome (4,6)
    StressCase("paraphrase", "the outcome was a rollback", 4, 6, group="g_outcome"),
    StressCase("paraphrase", "the result was a rollback", 4, 6, group="g_outcome"),
    StressCase("paraphrase", "the consequence was a rollback", 4, 6, group="g_outcome"),
    StressCase(
        "paraphrase",
        "in the end we rolled back",
        4,
        6,
        group="g_outcome",
        note="no outcome trigger word → likely widen",
    ),
    # g_milestone — temporal milestones (9,2)
    StressCase("paraphrase", "we hit the milestone of full offline", 9, 2, group="g_milestone"),
    StressCase(
        "paraphrase", "we reached the achievement of full offline", 9, 2, group="g_milestone"
    ),
    StressCase("paraphrase", "full offline was a major milestone", 9, 2, group="g_milestone"),
    StressCase(
        "paraphrase",
        "we accomplished full offline this week",
        9,
        2,
        group="g_milestone",
        note="'accomplished' synonym not in grammar → likely widen",
    ),
    # g_artifact — dev artifacts (11,0)
    StressCase("paraphrase", "the build produced an artifact", 11, 0, group="g_artifact"),
    StressCase("paraphrase", "the build emitted a binary", 11, 0, group="g_artifact"),
    StressCase("paraphrase", "the build output landed in dist", 11, 0, group="g_artifact"),
    StressCase(
        "paraphrase",
        "the build generated a deliverable package",
        11,
        0,
        group="g_artifact",
        note="'deliverable/package' synonym not in grammar → likely widen",
    ),
    # g_tool — tools cognitive (5,0)
    StressCase("paraphrase", "the web tool fetches a page", 5, 0, group="g_tool"),
    StressCase("paraphrase", "the fetch tool retrieves a URL", 5, 0, group="g_tool"),
    StressCase("paraphrase", "the search tool queries the index", 5, 0, group="g_tool"),
    StressCase(
        "paraphrase",
        "the browse utility summarizes a page",
        5,
        0,
        group="g_tool",
        note="'utility' synonym + browse present → check",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Angle 7 — noise: perturbations of known-good write cases. expected_y matches
# the clean case — we measure routing RETENTION (None where the trigger is
# genuinely destroyed by a typo is the correct/expected loss).
# ─────────────────────────────────────────────────────────────────────────────
NOISE: List[StressCase] = [
    StressCase("noise", "OUR GOAL IS TO SHIP DETERMINISTIC ROUTING", 3, 0, note="upper-case"),
    StressCase("noise", "our   goal    is   to ship routing", 3, 0, note="extra whitespace"),
    StressCase("noise", "our gaol is to ship routing", 3, None, note="typo 'gaol' → trigger lost"),
    StressCase("noise", "goal:ship", 3, 0, note="no spaces around colon"),
    StressCase("noise", "Goal — ship deterministic routing", 3, 0, note="em-dash"),
    StressCase("noise", "i am noah", 0, 0, note="all lower-case"),
    StressCase("noise", "I AM NOAH", 0, 0, note="all upper"),
    StressCase("noise", "I  am   Noah,  the  operator", 0, 0, note="whitespace"),
    StressCase("noise", "we   decided    to retire setfit", 3, 1, note="whitespace"),
    StressCase("noise", "we DECIDED to retire setfit", 3, 1, note="mixed case"),
    StressCase("noise", "the OUTCOME was a rollback", 4, 6, note="mixed case"),
    StressCase("noise", "the outcom was a rollback", 4, None, note="typo 'outcom' → lost"),
    StressCase("noise", "i recogniize this face", 7, 1, note="typo in non-trigger, 'face' intact"),
    StressCase("noise", "i recognize this fcae", 7, None, note="typo in trigger 'face' → lost"),
    StressCase("noise", "the session  started", 9, 0, note="whitespace, trigger intact"),
    StressCase("noise", "the SESSION started at noon", 9, 0, note="upper trigger"),
    StressCase(
        "noise",
        "a hard-line: no telemetry",
        0,
        None,
        note="'hard-line' hyphenated vs grammar 'hard line' → trigger lost",
    ),
    StressCase(
        "noise",
        "the buildd produced an artifact",
        11,
        0,
        note="typo non-trigger, 'artifact' intact",
    ),
    StressCase(
        "noise", "the artifactt is in dist", 11, None, note="typo in trigger 'artifact' → lost"
    ),
    StressCase(
        "noise",
        "privacy is non negotiable",
        0,
        None,
        note="'non negotiable' unhyphenated vs grammar 'non-negotiable' → lost",
    ),
    StressCase("noise", "we hit the MILESTONE of 1500 tests", 9, 2, note="upper trigger"),
    StressCase("noise", "the   web_fetch   tool   gets a url", 5, 0, note="whitespace"),
    StressCase("noise", "I FELT frustrated debugging", 4, 7, note="upper trigger"),
    StressCase("noise", "the deploy tool   ships   the   build", 5, 1, note="whitespace"),
    StressCase("noise", "store this Face embedding", 7, 1, note="capital F, trigger intact"),
    StressCase(
        "noise",
        "the progres update is green",
        11,
        1,
        note="typo 'progres' but 'update' is also a progress trigger → still routes",
    ),
    StressCase("noise", "the progress  update is green", 11, 1, note="whitespace, trigger intact"),
    StressCase("noise", "WHO approved the change", 4, 0, note="upper interrogative"),
    StressCase("noise", "why  did the worker hang", 4, 4, note="whitespace"),
    StressCase("noise", "the scene  shows a desk", 7, 2, note="whitespace"),
    StressCase("noise", "a CONSTRAINT: one operator only", 3, 2, note="upper trigger"),
    StressCase("noise", "the timelime shows regressions", 9, None, note="typo 'timelime' → lost"),
    StressCase("noise", "the timeline  shows regressions", 9, 3, note="whitespace, intact"),
    StressCase("noise", "you must NEVER delete memories", 0, 4, note="upper trigger"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Angle 8 — negation / hypothetical: keyword carriers that should NOT route by
# the surface keyword. The grammar is purely lexical → it WILL over-trigger;
# expected_y is None (ideal=widen), so mismatches quantify the over-trigger rate.
# ─────────────────────────────────────────────────────────────────────────────
NEGATION: List[StressCase] = [
    StressCase(
        "negation",
        "this is not a goal, just a stray thought",
        3,
        None,
        note="'goal' → goals(0); ideal=widen",
    ),
    StressCase(
        "negation",
        "we have no constraints here at all",
        3,
        None,
        note="'constraints' negated → constraints(2)",
    ),
    StressCase(
        "negation",
        "imagine if we decided to use a model",
        3,
        None,
        note="hypothetical 'decided' → decisions(1)",
    ),
    StressCase(
        "negation",
        "there were no open questions left",
        0,
        None,
        note="'open questions' negated → self_inquiry(5)",
    ),
    StressCase(
        "negation",
        "I never feel frustrated by clean code",
        4,
        None,
        note="'feel/frustrated' positive → sentiment(7)",
    ),
    StressCase(
        "negation", "we did not deploy anything today", 5, None, note="'deploy' negated → dev(1)"
    ),
    StressCase(
        "negation",
        "no face was detected in the frame",
        7,
        None,
        note="'face' negated → face_embeddings(1)",
    ),
    StressCase(
        "negation",
        "the session never actually started",
        9,
        None,
        note="'session' present → sessions(0)",
    ),
    StressCase(
        "negation",
        "this is not about my traits at all",
        0,
        None,
        note="'traits' negated → emergent_traits(2)",
    ),
    StressCase(
        "negation",
        "suppose the outcome had been different",
        4,
        None,
        note="hypothetical 'outcome' → outcome(6)",
    ),
    StressCase(
        "negation",
        "we never set any milestone for this",
        9,
        None,
        note="'milestone' negated → milestones(2)",
    ),
    StressCase(
        "negation",
        "had we chosen a model, it would be slower",
        3,
        None,
        note="counterfactual 'chosen' → decisions(1)",
    ),
    StressCase(
        "negation", "there is no roadmap yet for v2", 11, None, note="'roadmap' negated → vision(3)"
    ),
    StressCase(
        "negation",
        "I would never call privacy non-negotiable lightly",
        0,
        None,
        note="'non-negotiable' inside qualifier → guardian(4)",
    ),
    StressCase(
        "negation",
        "we don't have any artifacts from this build",
        11,
        None,
        note="'artifacts' negated → artifacts(0)",
    ),
    StressCase(
        "negation",
        "if only we had a heuristic for this",
        3,
        None,
        note="hypothetical 'heuristic' → heuristics(3)",
    ),
    StressCase(
        "negation",
        "no event occurred during the window",
        9,
        None,
        note="'event' negated → events(1)",
    ),
    StressCase(
        "negation",
        "this is hardly what I'd call alignment",
        3,
        None,
        note="dismissive 'alignment' → alignment(4)",
    ),
    StressCase(
        "negation",
        "we have not decided who approves changes",
        3,
        None,
        note="'decided' present (negated) → decisions(1)",
    ),
    StressCase(
        "negation",
        "there was no scene worth capturing",
        7,
        None,
        note="'scene' negated → scene_descriptions(2)",
    ),
    StressCase(
        "negation",
        "I wouldn't say the result was a failure",
        4,
        None,
        note="'result' present → outcome(6)",
    ),
    StressCase(
        "negation",
        "no tool can fetch that offline",
        5,
        None,
        note="'tool/fetch' present → cognitive(0); arguably fine",
    ),
    StressCase(
        "negation",
        "we shouldn't treat this as a constraint",
        3,
        None,
        note="'constraint' present → constraints(2)",
    ),
    StressCase(
        "negation",
        "it's not like we have a clear goal here",
        3,
        None,
        note="'goal' present → goals(0)",
    ),
    StressCase(
        "negation",
        "I don't recognize that face",
        7,
        None,
        note="'recognize/face' negated → face_embeddings(1)",
    ),
    StressCase(
        "negation",
        "there's no progress to report",
        11,
        None,
        note="'progress' negated → progress(1)",
    ),
    StressCase(
        "negation",
        "we never compile on the main branch",
        5,
        None,
        note="'compile' negated → dev(1)",
    ),
    StressCase(
        "negation",
        "not every session needs a milestone",
        9,
        None,
        note="'session' first → sessions(0)",
    ),
]


def all_stress_cases() -> List[StressCase]:
    """Every stress case across all angles, in a stable order."""
    cases: List[StressCase] = []
    cases.extend(WRITE_STATIC)
    cases.extend(WRITE_DYNAMIC_KW)
    cases.extend(WRITE_DYNAMIC_NAT)
    cases.extend(RECALL_QUERY)
    cases.extend(SYMMETRY_REALISTIC)
    cases.extend(CROSS_LOBE)
    cases.extend(PARAPHRASE)
    cases.extend(NOISE)
    cases.extend(NEGATION)
    return cases


ANGLES = (
    "write_static",
    "write_dynamic_kw",
    "write_dynamic_nat",
    "recall_query",
    "symmetry_realistic",
    "cross_lobe",
    "paraphrase",
    "noise",
    "negation",
)
