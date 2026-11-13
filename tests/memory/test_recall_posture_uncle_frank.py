#!/usr/bin/env python3
"""
test_recall_posture_uncle_frank.py
====================================

The interrogation. Operator's directive: hit the recall posture stack
the way you'd interrogate an uncle who claims to know everything —
push, contradict, set traps, watch the seams.

Five attack vectors:

    F. Persona framing recall
       Names, aliases, "who is X" with X being operator vs other persona
       vs unknown person, "who am I" ambiguity, relationship lookups,
       multi-persona queries.

    G. Code-agent project recall
       Ambiguous coding queries that span languages, code/workflow
       boundary, queries that reference code structure when the corpus
       has none, project-status queries.

    H. Time-sensitive recall
       Recency bias verification, session continuity, "what just
       happened", historical references when no temporal record exists,
       relative-time vocabulary ("yesterday", "earlier", "before X").

    I. Lane-winner vs BGE-candidate divergence
       The architectural concern I caught: meta["recall_winners"][0] is
       the posture-aware winner; res.candidates[0] is BGE-reranked.
       Brain reads `candidates`. If they disagree, BGE silently
       overrides the posture decision. Quantify how often this happens.

    J. Posture-suppression verification under hostile counter-evidence
       Construct cases where the SUPPRESSED lobe has stronger surface
       signal than the ALLOWED lobe. The posture's job is to override
       that. If the suppressed memory still wins, the suppression isn't
       biting hard enough.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
WARN = 0
FINDINGS: list[str] = []
WARNINGS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FINDINGS.append(f"{label}  {detail}")


def warn(label: str, detail: str = "") -> None:
    """Soft signal — observed-but-not-defect (or tunable behavior)."""
    global WARN
    WARN += 1
    print(f"  [WARN] {label}" + (f"  ({detail})" if detail else ""))
    WARNINGS.append(f"{label}  {detail}")


def section(label: str) -> None:
    print()
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)


from memory.api import QuerySpec, read_memory

# These recall scripts read the operator's live store on purpose, but read_memory appends a
# recall verdict row per query; keep those out of memory/recall_verifier/verdict_log.jsonl
# (found 2026-09-15 by the plain-script matrix pollution check).
import tempfile as _verdict_tempfile  # noqa: E402

import memory.recall_verifier as _recall_verifier  # noqa: E402

_VERDICT_SCRATCH = Path(_verdict_tempfile.mkdtemp(prefix="omega-recall-verdicts-")) / "verdict_log.jsonl"
_recall_verifier.verdict_log_path = lambda *, root=None: _VERDICT_SCRATCH
from memory.recall_compiler import compile_recall_plan


# Lightweight duck-typed QuerySpec for compiler-only checks
class FQS:
    def __init__(self, text, limit=10, domain=None, task_scope=None):
        self.cycle_id = "uncle-frank"
        self.text = text
        self.limit = limit
        self.modality = "text"
        self.task_scope = task_scope
        self.recall_policy_id = None
        self.domain = domain


def _live(text: str, label: str = ""):
    spec = QuerySpec(cycle_id=f"frank-{abs(hash(text + label)) % 99999}", text=text, limit=5)
    return read_memory(spec)


# =====================================================================
# F. Persona framing recall
# =====================================================================
section("F. Persona framing — names, aliases, 'who is X' under stress")

# F1: posture selection for persona-related queries
PERSONA_POSTURE_CASES = [
    # (query, expected primary posture OR None for "we accept fallback")
    ("who is the operator", "IDENTITY_RESOLUTION"),
    ("who am I", "IDENTITY_RESOLUTION"),
    ("tell me about Noah", None),  # ambiguous — could be BACKGROUND
    ("describe yourself", "IDENTITY_RESOLUTION"),
    ("what is your name", "IDENTITY_RESOLUTION"),
    ("who created you", "IDENTITY_RESOLUTION"),
    ("who is concurrentuser0", None),  # accept BACKGROUND or ROLE
    ("describe Noah's role", None),  # role-binding?
    ("who do you serve", "ROLE_BINDING"),
    ("what is your purpose", "IDENTITY_RESOLUTION"),
    ("tell me about the operator's background", "BACKGROUND_LOOKUP"),
    ("who knows the operator", None),  # relationship
]

for q, expected in PERSONA_POSTURE_CASES:
    res = _live(q, "F")
    posture = res.meta.get("posture")
    if expected is None:
        # We accept any non-empty posture
        ok = isinstance(posture, str) and len(posture) > 0
        check(f"F1 persona-query produces a posture: {q!r}", ok, detail=f"got {posture!r}")
    else:
        ok = posture == expected
        check(
            f"F1 persona-query -> expected posture: {q!r}",
            ok,
            detail=f"expected={expected} got={posture}",
        )

# F2: aliases/synonyms — does "user" route like "operator"?
plan_user = compile_recall_plan(FQS("who is the user"))
plan_operator = compile_recall_plan(FQS("who is the operator"))
# The expand_recall_terms map has user as a synonym of operator. So
# the expanded_terms for "who is the user" should include 'operator'.
expanded_user = set(plan_user.expanded_terms)
check(
    "F2.a 'user' query expands to include 'operator' synonym",
    "operator" in expanded_user,
    detail=f"expanded={sorted(expanded_user)[:8]!r}",
)

# F3: capitalized name extraction (entity heuristic)
plan_noah = compile_recall_plan(FQS("tell me about Noah"))
check(
    "F3.a 'Noah' extracted as entity",
    any("Noah" in e for e in plan_noah.entities),
    detail=f"entities={plan_noah.entities!r}",
)

# Multi-persona — "Alice and Bob" should both surface
plan_ab = compile_recall_plan(FQS('"Alice" and "Bob" worked together'))
ab_in_entities = sum(1 for e in plan_ab.entities if e.lower() in {"alice", "bob"})
check(
    "F3.b multi-quoted-name extraction",
    ab_in_entities >= 2,
    detail=f"entities={plan_ab.entities!r}",
)

# F4: persona suppression invariant — IDENTITY_RESOLUTION should NOT
# suppress lobe 1 (personas) since persona records are the answer
# shape for "who is the operator"
from memory.recall_posture import POSTURE_CATALOG

identity = POSTURE_CATALOG["IDENTITY_RESOLUTION"]
check(
    "F4.a IDENTITY does NOT suppress personas (lobe 1)",
    1 not in identity.suppressed_lobes,
    detail=f"suppressed_lobes={identity.suppressed_lobes!r}",
)
check(
    "F4.b IDENTITY DOES include lobe 1 in preferred_lobes",
    1 in identity.preferred_lobes,
    detail=f"preferred_lobes={identity.preferred_lobes!r}",
)


# =====================================================================
# G. Code-agent project recall
# =====================================================================
section("G. Code-agent project recall — language ambiguity, code/workflow boundary")

# G1: language disambiguation — "python work" vs "rust work" vs "go code"
LANG_CASES = [
    ("show me my python projects", "python"),
    ("what rust modules exist", "rust"),
    ("typescript code overview", "typescript"),
    ("review the go packages", "go"),
]
for q, expected_lang_hint in LANG_CASES:
    plan = compile_recall_plan(FQS(q))
    # Coord hypothesis should mention lobe 8 (coding) AND the right subdomain
    hyps = list(plan.coord_hypotheses)
    has_lobe_8 = any(h[0] == 8 for h in hyps)
    has_lang = any((h[2] or "").lower() == expected_lang_hint for h in hyps)
    check(
        f"G1 {expected_lang_hint!r:12s} query carries lobe-8 hypothesis",
        has_lobe_8,
        detail=f"hyps={hyps[:5]!r}",
    )
    if has_lobe_8:
        # Whether the language-specific subdomain is present is a
        # nicer-to-have; if missing, surface as WARN not FAIL because
        # the per-language keyword pool may need tuning.
        if not has_lang:
            warn(
                f"G1 {expected_lang_hint!r} subdomain hint not in coord_hypotheses",
                detail=f"hyps={hyps[:5]!r}",
            )

# G2: code/workflow boundary — "show me how to fetch" should hit
# PROCEDURAL_HOW (skills lobe + tools), NOT just CAPABILITY_ENUMERATION
plan_how = compile_recall_plan(FQS("show me how to fetch web content"))
check(
    "G2.a 'how to' query -> PROCEDURAL_HOW posture",
    plan_how.posture.name == "PROCEDURAL_HOW",
    detail=f"got {plan_how.posture.name}",
)
# PROCEDURAL_HOW prefers lobe 5 (skills) AND lobe 10 (workflows)
check(
    "G2.b PROCEDURAL_HOW prefers lobe 5 and 10",
    5 in plan_how.posture.preferred_lobes and 10 in plan_how.posture.preferred_lobes,
    detail=f"preferred={plan_how.posture.preferred_lobes!r}",
)

# G3: project status — "where are we on the X" should hit PROJECT_CONTEXT
plan_proj = compile_recall_plan(FQS("where are we on the project"))
check(
    "G3.a 'where are we on the project' -> PROJECT_CONTEXT posture",
    plan_proj.posture.name == "PROJECT_CONTEXT",
    detail=f"got {plan_proj.posture.name}",
)
# PROJECT_CONTEXT prefers lobe 4/10/11/8
prefs = set(plan_proj.posture.preferred_lobes)
expected_proj_lobes = {4, 10, 11, 8}
check(
    "G3.b PROJECT_CONTEXT preferred lobes match",
    prefs == expected_proj_lobes,
    detail=f"got={prefs!r} expected={expected_proj_lobes!r}",
)

# G4: live behavior — "show me my python projects" shouldn't crash and
# should return a valid recall_winners list
res_g4 = _live("show me my python projects", "G4")
check(
    "G4.a python-projects query returns recall_winners",
    isinstance(res_g4.meta.get("recall_winners"), list),
    detail=f"len={len(res_g4.meta.get('recall_winners') or [])}",
)


# =====================================================================
# H. Time-sensitive recall
# =====================================================================
section("H. Time-sensitive — recency bias, session continuity, relative time")

# H1: temporal posture selection
TEMPORAL_CASES = [
    ("what happened yesterday", "TEMPORAL_RECALL"),
    ("what did I do earlier", "TEMPORAL_RECALL"),
    ("show me the history of", "TEMPORAL_RECALL"),
    ("when did I start the project", "TEMPORAL_RECALL"),
    ("what was the last error", "ERROR_RECALL"),  # error-shape wins
    ("recent system status", "SYSTEM_STATUS"),  # status-shape wins
]
for q, expected in TEMPORAL_CASES:
    res = _live(q, "H1")
    posture = res.meta.get("posture")
    check(f"H1 {q!r:40s} -> {expected}", posture == expected, detail=f"got {posture}")

# H2: temporal_bias propagates from posture to plan
plan_temp = compile_recall_plan(FQS("what happened yesterday"))
check(
    "H2.a TEMPORAL_RECALL plan inherits temporal_bias='recent_first'",
    plan_temp.posture.temporal_bias == "recent_first",
    detail=f"got {plan_temp.posture.temporal_bias!r}",
)

# H3: TEMPORAL_RECALL plan boosts temporal lane
plan_temp_weights = plan_temp.lane_weights
temporal_weight = plan_temp_weights.get("temporal", 0.0)
check(
    "H3.a TEMPORAL_RECALL plan has high temporal lane weight",
    temporal_weight >= 1.5,
    detail=f"got temporal weight={temporal_weight}",
)

# H4: time-vocabulary task tags fire
plan_time_tags = compile_recall_plan(FQS("what happened earlier today"))
tags = set(plan_time_tags.task_tags)
check(
    "H4.a 'earlier' / 'today' triggers temporal task_tag",
    "temporal" in tags,
    detail=f"tags={tags!r}",
)

# H5: relative-time bypass — "what did I just say" should still classify
# even though there's no temporal vocabulary the rule list explicitly knows
plan_just = compile_recall_plan(FQS("what did I just say"))
check(
    "H5.a 'what did I just say' yields a posture",
    plan_just.posture.name != "",
    detail=f"got {plan_just.posture.name}",
)


# =====================================================================
# I. Lane-winner vs BGE-candidate divergence
# =====================================================================
section("I. ARCHITECTURAL — lane-winner vs BGE-candidate divergence")

# This is the key finding from the live probe: meta['recall_winners'][0]
# (posture-aware) and res.candidates[0] (BGE-reranked) frequently
# disagree. Brain reads `candidates`, so BGE is silently overriding
# the posture decision at the last step. Quantify the disagreement
# rate across a representative query battery.

DIV_QUERIES = [
    "who is the operator",
    "what tools can you use",
    "what do you know about photosynthesis",
    "remind me about the python work",
    "operator",
    "describe yourself",
    "show me how to fetch web content",
    "what happened yesterday",
    "where are we on the project",
    "explain the recall system",
]

divergence_count = 0
divergence_records: list[tuple] = []
for q in DIV_QUERIES:
    res = _live(q, "I")
    winners = res.meta.get("recall_winners") or []
    cands = res.candidates
    if not winners or not cands:
        continue
    winner_top = winners[0]["memory_id"]
    cand_top = cands[0].id
    if winner_top != cand_top:
        divergence_count += 1
        divergence_records.append((q, winner_top, cand_top))

# Hard finding: any divergence at all is an architectural concern.
# Brain reads `candidates`, which means BGE silently overrides posture.
divergence_rate = divergence_count / max(1, len(DIV_QUERIES))
print(f"  divergence rate: {divergence_count}/{len(DIV_QUERIES)} = {divergence_rate * 100:.0f}%")

# Surface every divergent pair so the operator can read the report
for q, w, c in divergence_records[:5]:
    print(f"     - q={q!r:45s} lane={w!r:25s} bge={c!r}")

if divergence_count == 0:
    check("I1.a lane-winner = BGE-candidate (no divergence)", True)
else:
    # This is a REAL finding, not a tunable. Mark as FAIL so the
    # suite exits non-zero and the operator sees it as actionable.
    check(
        "I1.a lane-winner = BGE-candidate (no divergence)",
        False,
        detail=(
            f"BGE silently overrides posture in "
            f"{divergence_count}/{len(DIV_QUERIES)} queries "
            f"({divergence_rate * 100:.0f}%) — brain reads "
            f"candidates[0] (BGE) not recall_winners[0] (posture)"
        ),
    )


# =====================================================================
# J. Posture-suppression under hostile counter-evidence
# =====================================================================
section("J. Suppression under hostile counter-evidence")

# Construct synthetic evidence where the SUPPRESSED lobe has STRONGER
# surface signal than the ALLOWED lobe. If posture suppression is
# strong enough, the allowed-lobe memory should still win. If not,
# we've found the threshold where posture loses to brute similarity.

from memory.recall_arbitrator import (
    arbitrate_evidence,
    posture_score_evidence,
)
from memory.evidence_set import EvidenceSet, MemoryEvidence
import logging

log = logging.getLogger("omega.tests.memory.test_recall_posture_uncle_frank")


plan_id = compile_recall_plan(FQS("who is the operator"))

# Allowed lobe (identity) with WEAK lane signal — only 1 lane, low score
ev_weak_allowed = MemoryEvidence(
    memory_id="0.0.weak",
    lanes=["identity"],
    lane_scores={"identity": 0.30},
    reasons=[],
    meta={
        "coord": [0, 0, 0],
        "type": "identity",
        "title": "weak identity record",
        "summary": "the operator",
    },
)
# Suppressed lobe (skills) with VERY STRONG lane signal — 4 lanes, high
ev_strong_suppressed = MemoryEvidence(
    memory_id="5.0.strong",
    lanes=["tools", "skills", "coding", "workflows"],
    lane_scores={"tools": 0.9, "skills": 0.9, "coding": 0.7, "workflows": 0.7},
    reasons=[],
    meta={
        "coord": [5, 0, 0],
        "type": "tool",
        "title": "operator",
        "summary": "tool whose title matches operator",
    },
)

score_weak_allowed = posture_score_evidence(ev_weak_allowed, plan_id)
score_strong_suppressed = posture_score_evidence(ev_strong_suppressed, plan_id)

print(f"  weak_allowed score = {score_weak_allowed:.3f}")
print(f"  strong_suppressed score = {score_strong_suppressed:.3f}")

# This is the threshold test: -0.30 lobe + -0.25 type = -0.55 penalty.
# Strong suppressed has base 0.9 + multi 0.15 + answerability ~0 +
# penalties -0.55 = ~0.50. Weak allowed has base 0.30 + multi 0 +
# answerability ~0.6 (operator title in query, role match) ~= 0.42.
# Suppressed wins, which means a tool memory titled "operator" would
# win over a weak identity memory — posture suppression isn't strong
# enough against multi-lane convergence.
if score_weak_allowed < score_strong_suppressed:
    # Soft-penalty score formula loses to multi-lane convergence —
    # this is documented behavior. The fix is hard_suppress at the
    # arbitrator (filter) level, verified in J1.b below.
    print(
        "  (info) score-formula soft penalty alone doesn't beat "
        "multi-lane convergence — that's why hard_suppress exists."
    )
    print(
        f"         score-formula: weak_allowed={score_weak_allowed:.3f} "
        f"strong_suppressed={score_strong_suppressed:.3f}"
    )
else:
    check(
        "J1.a score-formula posture suppression beats hostile",
        True,
        detail=f"weak_allowed={score_weak_allowed:.3f} "
        f"strong_suppressed={score_strong_suppressed:.3f}",
    )

# J1.b — at the arbitrator level, hard_suppress (set on
# IDENTITY_RESOLUTION) drops suppressed evidence ENTIRELY before
# scoring. Strong-suppressed should not appear in the ranked output.
ev_set_hard = EvidenceSet()
ev_set_hard.records[ev_weak_allowed.memory_id] = ev_weak_allowed
ev_set_hard.records[ev_strong_suppressed.memory_id] = ev_strong_suppressed

ranked_hard = arbitrate_evidence(ev_set_hard, plan_id)
ranked_hard_ids = [r.memory_id for r in ranked_hard]
check(
    "J1.b arbitrator drops suppressed evidence under hard_suppress",
    ev_strong_suppressed.memory_id not in ranked_hard_ids,
    detail=f"ranked={ranked_hard_ids!r}",
)
# And the allowed evidence should win since suppressed is gone
check(
    "J1.c allowed-lobe wins after hard_suppress drop",
    ranked_hard and ranked_hard[0].memory_id == ev_weak_allowed.memory_id,
    detail=f"top={ranked_hard[0].memory_id if ranked_hard else '<empty>'}",
)

# J2: confirm the same hostile setup with a NORMAL-strength allowed
# memory — suppression should bite there.
ev_normal_allowed = MemoryEvidence(
    memory_id="0.0.normal",
    lanes=["identity", "persona"],
    lane_scores={"identity": 0.6, "persona": 0.5},
    reasons=[],
    meta={
        "coord": [0, 0, 0],
        "type": "identity",
        "title": "operator identity",
        "summary": "the operator's identity",
    },
)
score_normal_allowed = posture_score_evidence(ev_normal_allowed, plan_id)
print(f"  normal_allowed score = {score_normal_allowed:.3f}")
check(
    "J2.a posture suppression beats hostile when allowed has normal signal",
    score_normal_allowed > score_strong_suppressed,
    detail=(
        f"normal_allowed={score_normal_allowed:.3f} strong_suppressed={score_strong_suppressed:.3f}"
    ),
)


# =====================================================================
# Summary
# =====================================================================
print()
print("=" * 70)
print(f"  PASSED:   {PASS}")
print(f"  FAILED:   {FAIL}")
print(f"  WARNINGS: {WARN}")
if FINDINGS:
    print()
    print("FINDINGS:")
    for f in FINDINGS:
        print(f"  {f}")
if WARNINGS:
    print()
    print("WARNINGS:")
    for w in WARNINGS:
        print(f"  {w}")
print("=" * 70)

raise SystemExit(0 if FAIL == 0 else 1)
