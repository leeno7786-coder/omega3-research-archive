"""The 1,000-case OUT-OF-CORPUS memory battery — terms set by the operator 2026-07-04.

RULES OF ENGAGEMENT
- 1,000 cases, all content DISJOINT from memory/routing/stress_corpus.py and
  from scripts/routing_grill_1000.py (both were tuned against; this is not).
- NO TUNING against this battery. It runs; the report is the report.
- Two invariants that cannot move:
    silent misroutes            == 0
    unverified upstream admissions == 0
- Live legs (findability / sense-alias / temporal-update) run through the REAL
  pipeline — write_memory (live embed when the brain server is up, skip_embed
  fallback recorded as model-surface degradation) -> reindex + graph compile ->
  production invoke_remember_tool + RecallVerifier — under an ISOLATED memory
  root. The operator's live store is never touched.

Report format (operator-specified, verbatim sections):
  TOTAL CASES / Core / Gate / Failure split
"""

from __future__ import annotations

import sys
import tempfile
import uuid
import importlib.util
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from auto_loop.mril_tools.router import route_memory_tool  # noqa: E402
from auto_loop.mril_tools.subdomain_helpers import subdomain_for  # noqa: E402
from memory.dewey_map import SUBDOMAIN_MAP  # noqa: E402

C = Counter()  # core + gate counters
FAIL = Counter()  # failure-split counters
DETAILS: List[str] = []


def note(kind: str, msg: str) -> None:
    DETAILS.append(f"[{kind}] {msg}")


# ═════════════════════════════ CASE GENERATION ══════════════════════════════
# All-new content: greenhouse ops, drone fleet, bakery, harbor logs, choir —
# nothing shared with the tuned corpora.


@dataclass(frozen=True)
class W:  # write-side case
    lobe: int
    y: Optional[int]  # None = widen expected
    text: str
    secondary: Tuple[Optional[int], ...] = ()  # label-ambiguity allowance


WRITE_FRAMES: List[W] = [
    # L0 identity (5)
    W(0, 0, "i value repairability over convenience in every purchase"),
    W(0, 4, "you must always verify a backup before deleting the source"),
    W(0, 2, "my defining characteristics are stubbornness and curiosity"),
    W(0, 3, "my self-model evolved after the greenhouse incident"),
    W(0, 1, "he is my cousin and keeps bees in the valley"),
    # L1 personas (5)
    W(1, 1, "Marcus repairs violins in the old quarter"),
    W(1, 0, "i answer messages fastest before sunrise"),
    W(1, 1, "she is my aunt and grows heirloom tomatoes"),
    W(1, 1, "Elena coaches the rowing team on saturdays"),
    W(1, 0, "i keep my calendar in plain text files"),
    # L3 reasoning (5)
    W(3, 0, "the objective is to automate the greenhouse vents"),
    W(3, 1, "we opted for drip irrigation over sprinklers"),
    W(3, 2, "the restriction is no pesticides near the hives"),
    W(3, 3, "our principle is to water before dawn"),
    W(3, 4, "soil health comes first in every planting choice"),
    # L4 situational (5)
    W(4, 6, "the harvest ended up doubling after the compost change"),
    W(4, 7, "there was real excitement when the seedlings sprouted"),
    W(4, 6, "the irrigation pump failed and we lost a row"),
    W(4, 7, "the volunteers felt proud after the market day"),
    W(4, 0, "who left the greenhouse door open is unclear"),
    # L5 tools (5)
    W(5, 0, "the moisture probe tool reads the soil hourly"),
    W(5, 1, "the seed catalog build pipeline runs nightly"),
    W(5, 0, "the wrapper around the weather endpoint caches results"),
    W(5, 1, "we track planting scripts in git for the coop"),
    W(5, 0, "the search tool indexes the almanac locally"),
    # L7 vision (5)
    W(7, 1, "a face appeared on the barn camera at dusk"),
    W(7, 2, "the scene shows the orchard after the storm"),
    W(7, 0, "the photo of the prize pumpkin is archived"),
    W(7, 3, "the frost patterns repeat along the fence line"),
    W(7, 0, "the monitor displays the hive temperature feed"),
    # L9 temporal (5)
    W(9, 0, "the planning session covered spring rotation"),
    W(9, 2, "we delivered the first honey batch on friday"),
    W(9, 1, "the hailstorm event happened before midnight"),
    W(9, 3, "the history of the orchard grafts spans decades"),
    W(9, 2, "the farm stand launched last weekend"),
    # L11 dev (5)
    W(11, 0, "the nightly build generated a fresh almanac bundle"),
    W(11, 1, "we still need to calibrate the rain gauge"),
    W(11, 3, "the roadmap targets full sensor coverage by fall"),
    W(11, 2, "the background setup runs on the barn server"),
    W(11, 1, "the status update says the fence is half painted"),
]


# angles applied to every write frame (12 per frame × 40 = 480 write cases)
def _angles(w: W) -> List[Tuple[str, str]]:
    p = w.text
    return [
        ("plain", p),
        ("embedded", f"worth noting somewhere: {p}"),
        ("quoted", f'the log entry read "{p}"'),
        ("past", f"as of last week, {p}"),
        ("future", f"from next season on, {p}"),
        ("filler", f"anyway, for the record, {p}"),
        ("runon", f"{p} and the cat slept through all of it"),
        ("caps", p.upper()),
        ("emoji", f"{p} 🌱"),
        ("whitespace", f"   {p}   "),
        ("clause", f"although the week was chaotic, {p}"),
        ("hedge", f"{p}, i believe"),
    ]


# negation — 50 FP probes (negated → widen) + 50 FN probes (must still route)
NEG_FP: List[W] = [
    W(3, None, "there is no objective for the fallow season"),
    W(3, None, "we never opted for chemical fertilizer"),
    W(4, None, "nobody felt excitement about the early frost"),
    W(5, None, "no tool reads the north field yet"),
    W(7, None, "no face showed up on the barn camera"),
    W(9, None, "the planning session never actually happened"),
    W(9, None, "we never delivered the second batch"),
    W(11, None, "the build never generated the bundle"),
    W(3, None, "it's not like we have a clear objective for the barn"),
    W(4, None, "there was no real outcome from the meeting"),
]
NEG_FN: List[W] = [
    W(3, 0, "the objective is important, not optional"),
    W(3, 2, "the restriction stands, no exceptions granted"),
    W(9, 0, "the session ran long, not that anyone minded"),
    W(4, 6, "the harvest ended up fine despite the scare"),
    W(11, 1, "progress is slow but not stalled"),
    W(5, 0, "the probe tool works, never mind the noise"),
    W(9, 2, "we delivered on friday, not saturday"),
    W(7, 0, "the photo is archived, not deleted"),
    W(0, 4, "you must never compost diseased leaves", (None,)),
    W(3, 1, "we opted out of sprinklers entirely"),
]

# paraphrase groups — 15 groups × 4 = 60; agreement REQUIRED on expected y
PARA: List[Tuple[int, int, List[str]]] = [
    (
        3,
        0,
        [
            "the aim is to double the herb yield",
            "our target is doubling the herb yield",
            "we intend to double the herb yield",
            "doubling the herb yield is the objective",
        ],
    ),
    (
        3,
        1,
        [
            "we chose cedar for the raised beds",
            "cedar was picked for the raised beds",
            "the choice for the raised beds was cedar",
            "we went with cedar on the raised beds",
        ],
    ),
    (
        4,
        7,
        [
            "everyone was nervous before the inspection",
            "there were real nerves ahead of the inspection",
            "the team felt anxious before the inspection",
            "anxiety ran high before the inspection",
        ],
    ),
    (
        9,
        2,
        [
            "we shipped the cider press order on monday",
            "the cider press order went out monday",
            "monday was when the cider press order was delivered",
            "the cider press order launched on monday",
        ],
    ),
    (
        11,
        1,
        [
            "we still need to patch the irrigation controller",
            "the irrigation controller patch is still pending",
            "patching the irrigation controller remains to-do",
            "the irrigation controller work is still underway",
        ],
    ),
    (
        7,
        1,
        [
            "a stranger's face was caught on the gate camera",
            "the gate camera caught an unfamiliar face",
            "an unknown face appeared at the gate feed",
            "the face on the gate camera was unfamiliar",
        ],
    ),
    (
        5,
        0,
        [
            "the almanac search tool answers season queries",
            "season queries go through the almanac search tool",
            "the search tool handles almanac season lookups",
            "almanac lookups run through the search tool",
        ],
    ),
    (
        0,
        4,
        [
            "you must never sell seeds without germination tests",
            "selling untested seeds is forbidden",
            "the rule is no seed sales without germination tests",
            "seed sales require germination tests, that is non-negotiable",
        ],
    ),
    (
        9,
        0,
        [
            "the tuesday session mapped the drainage plan",
            "drainage planning filled the tuesday session",
            "the session on tuesday was all drainage mapping",
            "we spent the tuesday session on the drainage plan",
        ],
    ),
    (
        3,
        2,
        [
            "the constraint is a two-acre water budget",
            "we are limited to a two-acre water budget",
            "a two-acre water budget is the hard limit",
            "the water budget restriction is two acres",
        ],
    ),
    (
        4,
        6,
        [
            "the grafting attempt failed on the old pear",
            "the old pear graft did not take and failed",
            "grafting the old pear ended in failure",
            "the pear grafting effort failed outright",
        ],
    ),
    (
        11,
        0,
        [
            "the sensor build produced a calibration file",
            "a calibration file came out of the sensor build",
            "the sensor build's output was a calibration file",
            "the build generated the sensor calibration file",
        ],
    ),
    (
        1,
        1,
        [
            "Priya keeps the orchard ledger balanced",
            "the orchard ledger is Priya's responsibility",
            "Priya manages the orchard ledger",
            "the ledger for the orchard is kept by Priya",
        ],
    ),
    (
        5,
        1,
        [
            "the deploy script pushes firmware to the pumps",
            "pump firmware ships via the deploy script",
            "the deploy pipeline delivers the pump firmware",
            "firmware for the pumps goes out through the deploy script",
        ],
    ),
    (
        0,
        0,
        [
            "i believe tools should outlast their makers",
            "my conviction is that tools should outlast their makers",
            "i hold that a tool should outlast its maker",
            "tools outlasting their makers is what i believe in",
        ],
    ),
]

# noise — 60 cases: perturbations of 20 new base facts (3 each)
NOISE_BASE: List[W] = [
    W(3, 1, "we settled on clay pots for the herb wall"),
    W(9, 1, "the power outage event hit the cold frame"),
    W(4, 7, "the crew was delighted with the seed swap"),
    W(5, 0, "the ledger tool tracks market sales"),
    W(7, 2, "the scene captures fog over the west field"),
    W(11, 3, "the roadmap includes a solar dryer"),
    W(0, 2, "my traits include patience with slow crops"),
    W(1, 1, "Tomas welds the trellis frames"),
    W(3, 4, "flavor matters more than shelf life here"),
    W(9, 3, "the history of the well repairs is documented"),
    W(4, 6, "the fermentation batch succeeded this time"),
    W(5, 1, "the compile step packages the sensor firmware"),
    W(7, 0, "the screen shows the incubator readout"),
    W(11, 2, "the setup context lives on the barn wiki"),
    W(0, 3, "my self-model evolved past the perfectionism"),
    W(3, 3, "our rule of thumb is taste before harvest"),
    W(9, 2, "the u-pick program went live in june"),
    W(4, 0, "who fed the goats saturday is unknown"),
    W(1, 0, "i sort correspondence at the kitchen table"),
    W(11, 1, "the fence progress report says two sides done"),
]


def _noise_variants(w: W) -> List[Tuple[str, str]]:
    p = w.text
    return [
        ("punct", p.replace(" ", "  ", 3) + " !!"),
        ("prefix", "um, quick note — " + p),
        ("suffix", p + " ... anyway."),
    ]


# term-match — 80 cases from the LIVE lobe-2/8 taxonomy (40 each), fresh templates
def _term_cases() -> List[W]:
    out: List[W] = []
    twos = sorted((y, n) for (x, y), n in SUBDOMAIN_MAP.items() if x == 2 and y != 127)[:40]
    eights = sorted((y, n) for (x, y), n in SUBDOMAIN_MAP.items() if x == 8 and y != 127)[:40]
    for y, name in twos:
        human = name.replace("_", " ")
        out.append(W(2, y, f"spent the evening reading about {human} again"))
    for y, name in eights:
        human = name.replace("_", " ")
        out.append(W(8, y, f"cleaned up the {human} handling in the sensor code"))
    return out


# recall lobe accuracy — 60 fresh queries with expected lobe (plausible sets)
RECALL_Q: List[Tuple[str, Tuple[int, ...]]] = [
    ("what did we decide about the raised beds", (3,)),
    ("what is the objective for the greenhouse vents", (3,)),
    ("what constraints apply to the water budget", (3,)),
    ("what is our rule of thumb before harvest", (3,)),
    ("what are our core values for planting", (3,)),
    ("what was the outcome of the compost change", (4,)),
    ("how did the volunteers feel after market day", (4,)),
    ("who left the greenhouse door open", (4, 0)),
    ("what tool reads the soil hourly", (5,)),
    ("which pipeline runs the seed catalog build", (5,)),
    ("what wraps the weather endpoint", (5,)),
    ("whose face was on the barn camera", (7,)),
    ("what scene did the orchard camera show", (7,)),
    ("where is the photo of the prize pumpkin", (7,)),
    ("what did the planning session cover", (9,)),
    ("when did we deliver the first honey batch", (9,)),
    ("what event happened before midnight", (9,)),
    ("what is the history of the orchard grafts", (9,)),
    ("where is the almanac bundle from the nightly build", (11,)),
    ("what progress remains on the rain gauge", (11,)),
    ("what does the roadmap target for fall", (11,)),
    ("what is the setup background for the barn server", (11,)),
    ("what are my defining characteristics", (0,)),
    ("what are the hard rules about backups", (0,)),
    ("who is my cousin with the bees", (0, 1)),
    ("who is Marcus", (1,)),
    ("who coaches the rowing team", (1, 2)),
    ("when do i answer messages fastest", (1, 0, 9)),
    ("what do you know about crop rotation", (2,)),
    ("what do you know about beekeeping seasons", (2,)),
    ("the session where we mapped the drainage", (9, 3)),
    ("why did the irrigation pump fail last night", (4, 5, 9)),
    ("the calibration file from the sensor build broke", (11, 8)),
    ("you should always test the backup power", (0, 3, 5)),
    ("the milestone was finishing the fence", (9, 3)),
    ("remember how the crew felt when the pump died", (4, 9, 0)),
    ("the aim of this session was drainage", (3, 9)),
    ("how do we patch the irrigation controller firmware", (5, 8, 10, 11)),
    ("what did the build produce last night", (11,)),
    ("how to prune the espalier correctly", (5, 8, 10)),
    ("what do you remember about the sensor codebase", (8, 11)),
    ("what do you remember about the implementation in the pump repo", (11, 8)),
    ("notes on the beehive inspection checklist we follow", (10, 5, 2)),
    ("what goals did we set for autumn", (3,)),
    ("which milestones landed this quarter", (9,)),
    ("what artifacts came out of the drone build", (11,)),
    ("which scenes did the drone capture", (7,)),
    ("what journal did deepsleep write", (6,)),
    ("who do i know named Priya", (1,)),
    ("who is my aunt with the tomatoes", (1, 0)),
    ("where do i work these days", (1,)),
    ("what happened last time we pruned", (9,)),
    ("open questions about the drone fleet", (0,)),
    ("what is my trajectory this season", (9,)),
    ("what was the result of the fermentation batch", (4,)),
    ("what cognitive tools handle almanac lookups", (5,)),
    ("what did we choose for the herb wall", (3,)),
    ("progress in mastery of grafting", (2,)),
    ("what do you remember about me", (1,)),
    ("why did we decide to water before dawn", (3,)),
]

# cross-lobe plausible — 30 fresh ambiguous statements
CROSS: List[Tuple[str, Tuple[int, ...]]] = [
    ("the session where we tasted the first cider", (9, 4)),
    ("i feel the drone fleet is overengineered", (4, 11, 5)),
    ("our principle is to log every hive visit", (3, 9)),
    ("why did the drone mapping fail over the pond", (4, 5, 9, 7)),
    ("the milestone was choosing the orchard layout", (9, 3)),
    ("a photo of the crew after the barn raising", (7, 9)),
    ("the artifact from the drone build cracked", (11, 8)),
    ("the constraint pushed us to smaller beds", (3,)),
    ("who decided the fence alignment", (3, 4)),
    ("the history of our irrigation choices", (9, 3)),
    ("you should never spray in high wind", (0, 3, 5)),
    ("remember how i felt when the hive swarmed", (4, 0, 9)),
    ("the goal of the tuesday session was drainage", (3, 9)),
    ("how do we debug the pump controller loop", (5, 8, 10, 11)),
    ("the scene from the drone showed a new nest", (7,)),
    ("we recorded the pruning workflow for newcomers", (10, 5, 9)),
    ("i decided to study soil chemistry", (0, 2, 3)),
    ("my aim is to learn cheese making", (0, 2, 3)),
    ("we built a rig to deploy the moisture probes", (5, 8, 11)),
    ("the recognition model mislabels the barn owl", (2, 7, 8)),
    ("what rig do we use to test pump firmware", (5, 8, 10)),
    ("my values shape which seeds i save", (0, 3)),
    ("the checklist steps for the market stall rollout", (10, 5)),
    ("the outage during friday's session", (9, 4)),
    ("who is Captain Reyes at the harbor office", (1,)),
    ("the quarterly report artifact is ready", (11, 9)),
    ("the choir felt flat before the recital", (4, 1)),
    ("suppose the drone had crashed into the silo", (2, 4, 11, 7)),
    ("the bakery order event doubled on sunday", (9, 4)),
    ("it's not like the harbor log has a clear owner", (2, 9, 3, 1)),
]

# ── LIVE legs ──
# findability roundtrips: 100 = the 40 frames' plain + 60 fresh pairs
LIVE_PAIRS: List[Tuple[W, str]] = [
    (w, q)
    for w, q in [
        (
            W(3, 1, "we opted for cedar shingles on the coop"),
            "what did we decide about the coop roof",
        ),
        (W(9, 2, "the seed library launched in march"), "when did the seed library launch"),
        (
            W(4, 6, "the cider pressing succeeded on the second try"),
            "what was the outcome of the cider pressing",
        ),
        (W(5, 0, "the frost alert tool watches the forecast"), "what tool watches the forecast"),
        (
            W(7, 1, "an unfamiliar face lingered near the toolshed"),
            "whose face was near the toolshed",
        ),
        (
            W(11, 1, "we still need to seal the rain barrels"),
            "what progress remains on the rain barrels",
        ),
        (W(0, 2, "my traits include impatience with waste"), "what are my traits about waste"),
        (W(1, 1, "Elena tallies the market receipts"), "who tallies the market receipts"),
        (
            W(3, 0, "the objective is a self-watering seed wall"),
            "what is the objective for the seed wall",
        ),
        (
            W(9, 0, "the sunday session planned the bee corridor"),
            "what did the sunday session plan",
        ),
    ]
] * 7  # 70 fresh pairs (rotations get unique suffixes at write time)

# sense/alias — 25 live pairs: recall uses NO shared keyword with the write
ALIAS_PAIRS: List[Tuple[W, str]] = [
    (
        W(3, 1, "we opted for rainwater collection over well drilling"),
        "what did we settle on for irrigation supply",
    ),
    (W(9, 2, "the honey stand went live on friday"), "when did the sweet stall open"),
    (
        W(4, 7, "the crew felt anxious before the health inspection"),
        "how was morale ahead of the audit",
    ),
    (
        W(5, 0, "the moisture probe tool reads the beds hourly"),
        "what gadget checks the soil dampness",
    ),
    (
        W(11, 0, "the nightly build produced the almanac bundle"),
        "where is the output package from last night",
    ),
] * 5  # 25 with unique suffixes

# temporal/update — 15 live triples: v1 write, v2 update, recall expects v2
UPDATE_TRIPLES: List[Tuple[int, int, str, str, str]] = [
    (
        3,
        1,
        "we decided the market stall opens at 8am",
        "update: we decided the market stall now opens at 7am",
        "what did we decide about the market stall opening time",
    ),
    (
        9,
        2,
        "the drone survey was scheduled for tuesday",
        "update: the drone survey moved to thursday",
        "when is the drone survey",
    ),
    (
        11,
        1,
        "the fence progress stands at one side done",
        "update: the fence progress is now three sides done",
        "what is the fence progress",
    ),
] * 5


# ═════════════════════════════ GRADERS ══════════════════════════════════════


def grade_write(w: W, text: str, bucket: str) -> None:
    got = subdomain_for(text, w.lobe)
    C[f"{bucket}_total"] += 1
    if w.y is None:  # widen expected (negation FP probes)
        if got is None:
            C[f"{bucket}_ok"] += 1
        else:
            C["neg_fp"] += 1
            FAIL["negation false positive"] += 1
            note("NEG-FP", f"L{w.lobe} got={got} :: {text!r}")
        return
    if got == w.y:
        C[f"{bucket}_ok"] += 1
    elif got is None:
        if bucket == "negfn":
            FAIL["negation false negative"] += 1
            note("NEG-FN", f"L{w.lobe} exp={w.y} widened :: {text!r}")
        else:
            C["write_widen"] += 1
            FAIL["write-side taxonomy miss"] += 1
            note("WIDEN", f"L{w.lobe} exp={w.y} :: {text!r}")
    elif got in w.secondary:
        FAIL["benchmark/label ambiguity"] += 1
    else:
        C["silent_misroute"] += 1
        FAIL["write-side taxonomy miss"] += 1
        note("SILENT-MISROUTE", f"L{w.lobe} exp={w.y} got={got} :: {text!r}")


def grade_recall_lobe(q: str, plausible: Tuple[int, ...]) -> None:
    C["recallq_total"] += 1
    got = route_memory_tool(q)["lobe"]
    if got in plausible:
        C["recallq_ok"] += 1
    else:
        FAIL["recall-side lobe miss"] += 1
        note("RECALL-LOBE", f"plausible={plausible} got=L{got} :: {q!r}")


# ═════════════════════════════ LIVE PIPELINE ═════════════════════════════════


def run_live() -> None:
    spec = importlib.util.spec_from_file_location("mlc", ROOT / "scripts" / "memory_live_check.py")
    mlc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mlc)

    with tempfile.TemporaryDirectory() as d:
        mlc._isolate_memory_root(Path(d))
        from auto_loop.mril_tools.registry import REMEMBER_TOOL_REGISTRY
        from auto_loop.mril_tools.remember import invoke_remember_tool
        from memory.api import CanonicalEnvelope, write_memory
        from memory.engine.reindex import reindex_and_compile

        tool_by_lobe = {lobe: t for t, (lane, lobe, mt) in REMEMBER_TOOL_REGISTRY.items()}

        def _write(lobe: int, y: int, text: str, tag: str) -> Optional[str]:
            sfx = uuid.uuid4().hex[:8]
            env = CanonicalEnvelope(
                cycle_id=f"battery-{tag}-{sfx}",
                coord=[lobe, y, 0],
                type="semantic",
                payload={"title": f"{tag}-{sfx}", "summary": text, "text": text},
                source="memory.api",
            )
            try:
                r = write_memory(env)
                C["live_embed_writes"] += 1
            except Exception:
                try:
                    r = write_memory(env, skip_embed=True)
                    C["skip_embed_writes"] += 1
                except Exception as exc:
                    FAIL["model surface failure"] += 1
                    note("WRITE-FAIL", f"L{lobe} {type(exc).__name__}: {exc}")
                    return None
            # WriteReceipt carries staging_ref/path, not a memory_id field —
            # the committed memory id is the path stem ({id}.json).
            if str(getattr(r, "validation_status", "")) == "rejected":
                note("WRITE-REJECTED", f"L{lobe} {getattr(r, 'message', '')} :: {text[:50]!r}")
                return None
            path = getattr(r, "path", None)
            if path:
                return Path(path).stem
            ref = getattr(r, "staging_ref", None)
            return str(ref) if ref else None

        def _recall(lobe: int, q: str, tag: str):
            tool = tool_by_lobe.get(lobe)
            entry = {
                "tool": tool,
                "query": q,
                "allowed_lobes": [lobe],
                "memory_type": REMEMBER_TOOL_REGISTRY[tool][2],
                "lane": REMEMBER_TOOL_REGISTRY[tool][0],
                "lobe": lobe,
            }
            y = subdomain_for(q, lobe)
            if y is not None:
                entry["subdomain"] = y
            try:
                return invoke_remember_tool(entry, f"battery-{tag}")
            except Exception as exc:
                FAIL["model surface failure"] += 1
                note("RECALL-FAIL", f"L{lobe} {type(exc).__name__}: {exc} :: {q!r}")
                return None

        def _gate(out: dict) -> None:
            v = str(out.get("verdict") or "unknown")
            ev = out.get("evidence") or []
            if v == "verified":
                C["gate_verified"] += 1
            elif v in ("unknown", "empty", ""):
                C["gate_unknown"] += 1
            elif v == "ambiguous":
                C["gate_ambiguous"] += 1
            elif v == "conflict":
                C["gate_conflict"] += 1
            else:
                C["gate_unknown"] += 1
            if ev and v != "verified":
                C["gate_unverified_upstream"] += 1
                note("UNVERIFIED-ADMISSION", f"verdict={v} evidence={len(ev)}")

        # -- findability (100) --
        pairs = []
        for i, (w, q) in enumerate(LIVE_PAIRS):
            sfx = f" (entry {i})"
            pairs.append((w.lobe, w.y, w.text + sfx, q, f"find{i}"))
        for w in WRITE_FRAMES:
            pairs.append((w.lobe, w.y, w.text, "", "frame"))
        written = []
        for lobe, y, text, q, tag in pairs:
            mid = _write(lobe, y, text, tag)
            if not q:
                continue  # frame writes only populate the store for realism
            C["find_total"] += 1
            if mid:
                written.append((lobe, text, q, mid))

        try:
            reindex_and_compile()
        except Exception as exc:
            FAIL["model surface failure"] += 1
            note("COMPILE-FAIL", f"{type(exc).__name__}: {exc}")

        for lobe, text, q, mid in written:
            out = _recall(lobe, q, f"rt-{mid[:8]}")
            if out is None:
                continue
            _gate(out)
            ev = out.get("evidence") or []
            hits = {str(e.get("memory_id")) for e in ev}
            blob = " ".join(str(e.get("remembered_as", "")) for e in ev).lower()
            if mid in hits or text[:30].lower() in blob:
                C["find_ok"] += 1
            else:
                FAIL["recall-side lobe miss"] += 1
                note("FIND-MISS", f"L{lobe} widened={out.get('widened_unsorted')} :: {q!r}")

        # -- sense/alias (25) --
        for i, (w, q) in enumerate(ALIAS_PAIRS):
            text = w.text + f" (alias {i})"
            mid = _write(w.lobe, w.y, text, f"alias{i}")
            C["alias_total"] += 1
            if not mid:
                continue
            try:
                reindex_and_compile()
            except Exception:
                pass
            out = _recall(w.lobe, q, f"alias-{i}")
            if out is None:
                continue
            _gate(out)
            ev = out.get("evidence") or []
            hits = {str(e.get("memory_id")) for e in ev}
            blob = " ".join(str(e.get("remembered_as", "")) for e in ev).lower()
            if mid in hits or text[:25].lower() in blob:
                C["alias_ok"] += 1
            else:
                FAIL["sense/alias miss"] += 1
                note("ALIAS-MISS", f"L{w.lobe} :: q={q!r}")

        # -- temporal/update (15) --
        for i, (lobe, y, v1, v2, q) in enumerate(UPDATE_TRIPLES):
            _write(lobe, y, v1 + f" (u{i})", f"upd{i}a")
            m2 = _write(lobe, y, v2 + f" (u{i})", f"upd{i}b")
            C["update_total"] += 1
            if not m2:
                continue
            try:
                reindex_and_compile()
            except Exception:
                pass
            out = _recall(lobe, q, f"upd-{i}")
            if out is None:
                continue
            _gate(out)
            ev = out.get("evidence") or []
            hits = [str(e.get("memory_id")) for e in ev]
            blob = " ".join(str(e.get("remembered_as", "")) for e in ev).lower()
            if (m2 in hits) or ("update:" in blob or v2[:28].lower() in blob):
                C["update_ok"] += 1
            else:
                FAIL["temporal/update miss"] += 1
                note("UPDATE-MISS", f"L{lobe} q={q!r} got={hits[:3]}")


# ═════════════════════════════ MAIN + REPORT ═════════════════════════════════


def main() -> None:
    total = 0

    for w in WRITE_FRAMES:  # 480 write-angle cases
        for _, text in _angles(w):
            grade_write(w, text, "write")
            total += 1
    for w in NEG_FP * 6:  # 60 negation FP probes
        grade_write(w, w.text, "negfp")
        total += 1
    for w in NEG_FN * 6:  # 60 negation FN probes
        grade_write(w, w.text, "negfn")
        total += 1
    for lobe, y, groups in PARA:  # 60 paraphrase
        for p in groups:
            grade_write(W(lobe, y, p), p, "para")
            total += 1
    for w in NOISE_BASE:  # 60 noise
        for _, t in _noise_variants(w):
            grade_write(w, t, "noise")
            total += 1
    for w in _term_cases():  # 80 term-match
        grade_write(w, w.text, "term")
        total += 1
    for q, pl in RECALL_Q:  # 60 recall-lobe
        grade_recall_lobe(q, pl)
        total += 1
    for q, pl in CROSS:  # 30 cross-lobe
        C["cross_total"] += 1
        got = route_memory_tool(q)["lobe"]
        if got in pl:
            C["cross_ok"] += 1
        else:
            FAIL["recall-side lobe miss"] += 1
            note("CROSS", f"plausible={pl} got=L{got} :: {q!r}")
        total += 1

    run_live()  # 100 find + 25 alias + 15 update
    total += C["find_total"] + C["alias_total"] + C["update_total"]

    def pct(ok, tot):
        return f"{ok}/{tot} ({100.0 * ok / tot:5.1f}%)" if tot else "n/a"

    write_ok = C["write_ok"] + C["para_ok"] + C["noise_ok"] + C["term_ok"] + C["negfn_ok"]
    write_tot = (
        C["write_total"] + C["para_total"] + C["noise_total"] + C["term_total"] + C["negfn_total"]
    )
    neg_tot = C["negfp_total"]

    print()
    print(f"TOTAL CASES: {total}")
    print()
    print("Core:")
    print(f"- write correctness            {pct(write_ok, write_tot)}")
    print(f"- recall lobe accuracy         {pct(C['recallq_ok'], C['recallq_total'])}")
    print(f"- findability                  {pct(C['find_ok'], C['find_total'])}")
    print(f"- silent misroutes             {C['silent_misroute']}   ◀ INVARIANT (must be 0)")
    print(
        f"- negation over-trigger        {pct(neg_tot - C['neg_fp'], neg_tot)}  ({C['neg_fp']} FPs)"
    )
    print(f"- paraphrase                   {pct(C['para_ok'], C['para_total'])}")
    print(f"- noise                        {pct(C['noise_ok'], C['noise_total'])}")
    print(f"- term-match                   {pct(C['term_ok'], C['term_total'])}")
    print(f"- cross-lobe plausible         {pct(C['cross_ok'], C['cross_total'])}")
    print()
    print("Gate:")
    print(f"- verified admissions          {C['gate_verified']}")
    print(f"- unknown returns              {C['gate_unknown']}")
    print(f"- ambiguous returns            {C['gate_ambiguous']}")
    print(f"- conflict returns             {C['gate_conflict']}")
    print(
        f"- unverified upstream admissions {C['gate_unverified_upstream']}   ◀ INVARIANT (must be 0)"
    )
    print()
    print("Failure split:")
    for k in (
        "write-side taxonomy miss",
        "recall-side lobe miss",
        "sense/alias miss",
        "temporal/update miss",
        "negation false positive",
        "negation false negative",
        "model surface failure",
        "benchmark/label ambiguity",
    ):
        print(f"- {k:<28} {FAIL[k]}")
    print()
    print(
        f"live embed writes: {C['live_embed_writes']}   skip-embed fallbacks: {C['skip_embed_writes']}"
    )
    inv_ok = C["silent_misroute"] == 0 and C["gate_unverified_upstream"] == 0
    print(f"\nINVARIANTS: {'HELD' if inv_ok else '*** VIOLATED ***'}")
    if DETAILS:
        print(f"\n-- details ({len(DETAILS)}) --")
        for m in DETAILS[:80]:
            print("  " + m)


if __name__ == "__main__":
    main()
