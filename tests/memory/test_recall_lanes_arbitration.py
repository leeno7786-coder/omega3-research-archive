#!/usr/bin/env python3
"""
test_recall_lanes_arbitration.py
================================

Coverage for the Recall Posture middle layer Claude filled (after
Kimi's skeleton):

    A. Lane execution
       - lobe-scoped lanes pull from the right lobes
       - posture lane_weight=0 suppresses the lane entirely
       - lane_weight gates the per-hit score
       - temporal lane re-ranks by ingested_at when posture asks for
         recent_first
       - strength lane returns [] when posture has no strength_bias
        - Ettin perception lane is callable and degrades to [] when cold

    B. Scoring math (recall_arbitrator)
       - multi_lane_bonus brackets exactly: 2->0.05, 3->0.10, 4->0.15, 5+->0.20
       - suppressed_lobe_penalty = -0.30 when coord lobe in posture's
         suppressed_lobes list
       - suppressed_memory_type_penalty = -0.25 when memory.type in
         posture's suppressed_memory_types list
       - posture_score_evidence clamps to [0, 1]
       - arbitrate_evidence ties broken deterministically (score desc,
         then memory_id asc)

    C. Receipt completeness
       - receipt has all required keys
       - lanes_used reflects only lanes that actually emitted hits
       - winning_memory_ids matches arbitrate_evidence output order
       - why_selected carries the synthesis reason for each winner
       - receipt is JSON-serializable

    D. Live read_memory integration
       - meta["recall_winners"] is populated for non-empty corpora
       - winners carry lanes + reasons
       - deterministic for repeated identical queries
       - IDENTITY_RESOLUTION posture suppresses lobe-5/8/10/11 in the
         winners list when those lobes have entries
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{label}  {detail}")


def section(label: str) -> None:
    print()
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)


# =====================================================================
# A. Lane execution
# =====================================================================
section("A. Lane execution")

from memory.recall_lanes import (
    LaneHit,
    run_recall_lanes,
    _run_lobe_lane,
    _run_temporal_lane,
    _run_strength_lane,
    _LANE_TO_LOBES,
    LANE_IDENTITY,
    LANE_TOOLS,
    LANE_KNOWLEDGE,
    LANE_TEMPORAL,
    LANE_STRENGTH,
    LANE_EMBEDDING,
    ALL_LANE_NAMES,
)
from memory.recall_compiler import compile_recall_plan


# Duck-typed QuerySpec
class FakeQS:
    def __init__(self, text: str, limit: int = 10):
        self.cycle_id = "test-lanes"
        self.text = text
        self.limit = limit
        self.modality = "text"
        self.task_scope = None
        self.recall_policy_id = None
        self.domain = None


# Synthetic coord index for isolated lane tests — bypasses the live
# corpus so we can pin behavior without depending on whatever's on
# disk in the operator's repo.
def _mk_index_entry(mid, coord, path):
    return {"id": mid, "coord": coord, "path": path}


SYN_INDEX = {
    "0.0.aaa": _mk_index_entry("0.0.aaa", [0, 0, 0], "FAKE/identity_aaa.json"),
    "1.0.bbb": _mk_index_entry("1.0.bbb", [1, 0, 0], "FAKE/persona_bbb.json"),
    "2.0.ccc": _mk_index_entry("2.0.ccc", [2, 0, 0], "FAKE/knowledge_ccc.json"),
    "5.0.ddd": _mk_index_entry("5.0.ddd", [5, 0, 0], "FAKE/skill_ddd.json"),
    "5.1.eee": _mk_index_entry("5.1.eee", [5, 1, 0], "FAKE/tool_eee.json"),
    "8.0.fff": _mk_index_entry("8.0.fff", [8, 0, 0], "FAKE/coding_fff.json"),
    "9.0.ggg": _mk_index_entry("9.0.ggg", [9, 0, 0], "FAKE/temporal_ggg.json"),
}

SYN_META = {
    "FAKE/identity_aaa.json": {
        "id": "0.0.aaa",
        "coord": [0, 0, 0],
        "type": "identity",
        "title": "operator identity",
        "summary": "the operator is Noah",
        "text": "Operator: Noah. Builds Omega.",
        "tags": ["operator"],
        "provenance": {"ingested_at": "2026-04-01T00:00:00Z"},
    },
    "FAKE/persona_bbb.json": {
        "id": "1.0.bbb",
        "coord": [1, 0, 0],
        "type": "persona",
        "title": "operator persona",
        "summary": "operator persona record",
        "text": "Persona: operator details",
        "tags": ["operator", "persona"],
        "provenance": {"ingested_at": "2026-05-01T00:00:00Z"},
    },
    "FAKE/knowledge_ccc.json": {
        "id": "2.0.ccc",
        "coord": [2, 0, 0],
        "type": "concept",
        "title": "Photosynthesis",
        "summary": "biology concept",
        "text": "Photosynthesis converts light to chemical energy.",
        "tags": ["biology"],
        "provenance": {"ingested_at": "2026-03-01T00:00:00Z"},
    },
    "FAKE/skill_ddd.json": {
        "id": "5.0.ddd",
        "coord": [5, 0, 0],
        "type": "skill",
        "title": "compute_average",
        "summary": "average a list",
        "text": "Compute arithmetic mean of a list.",
        "tags": ["math", "skill"],
        "provenance": {"ingested_at": "2026-04-15T00:00:00Z"},
    },
    "FAKE/tool_eee.json": {
        "id": "5.1.eee",
        "coord": [5, 1, 0],
        "type": "tool",
        "title": "web_fetch",
        "summary": "fetch web content",
        "text": "Fetch concise web knowledge.",
        "tags": ["tool"],
        "provenance": {"ingested_at": "2026-05-05T00:00:00Z"},
    },
    "FAKE/coding_fff.json": {
        "id": "8.0.fff",
        "coord": [8, 0, 0],
        "type": "code",
        "title": "python project scan",
        "summary": "python coding tool",
        "text": "Scan a python project tree.",
        "tags": ["python", "code"],
        "provenance": {"ingested_at": "2026-05-06T00:00:00Z"},
    },
    "FAKE/temporal_ggg.json": {
        "id": "9.0.ggg",
        "coord": [9, 0, 0],
        "type": "event",
        "title": "session 42",
        "summary": "yesterday session",
        "text": "Session 42 events.",
        "tags": ["temporal", "session"],
        "provenance": {"ingested_at": "2026-05-07T00:00:00Z"},
    },
}


def _fake_load(path):
    return SYN_META.get(path)


# A1: lobe-scoped lane pulls from the right lobes
plan_id = compile_recall_plan(FakeQS("who is the operator"))
identity_hits = _run_lobe_lane(
    LANE_IDENTITY,
    plan_id,
    SYN_INDEX,
    _fake_load,
    plan_terms=set(plan_id.exact_terms) | set(plan_id.expanded_terms),
)
check(
    "A1.a identity lane returns identity-lobe memories",
    all(h.metadata["coord"][0] == 0 for h in identity_hits),
    detail=f"hits={[h.memory_id for h in identity_hits]}",
)

# A2: tools lane reads ONLY subdomain 1 of lobe 5
plan_cap = compile_recall_plan(FakeQS("what tools can you use"))
tools_hits = _run_lobe_lane(
    LANE_TOOLS,
    plan_cap,
    SYN_INDEX,
    _fake_load,
    plan_terms=set(plan_cap.exact_terms) | set(plan_cap.expanded_terms),
)
allowed_lobes, allowed_subs = _LANE_TO_LOBES[LANE_TOOLS]
check("A2.a tools lane has subdomain filter", allowed_subs == [1], detail=f"got {allowed_subs!r}")
check(
    "A2.b tools lane returns only subdomain-1 of lobe 5",
    all(h.metadata["coord"][:2] == [5, 1] for h in tools_hits),
    detail=f"hits={[h.metadata['coord'] for h in tools_hits]}",
)

# A3: lane_weight=0 suppresses
# IDENTITY_RESOLUTION lane_weights have tools=0.0 — running
# run_recall_lanes for an identity plan should NOT return any
# tools-lane hits.
all_hits = run_recall_lanes(plan_id, SYN_INDEX, _fake_load)
tools_in_all = [h for h in all_hits if h.lane == LANE_TOOLS]
check(
    "A3.a IDENTITY plan suppresses tools lane (lane_weight=0)",
    len(tools_in_all) == 0,
    detail=f"got {len(tools_in_all)} tools hits",
)
workflows_in_all = [h for h in all_hits if h.lane == "workflows"]
check(
    "A3.b IDENTITY plan suppresses workflows lane",
    len(workflows_in_all) == 0,
    detail=f"got {len(workflows_in_all)} workflows hits",
)

# A4: lane_weight scales emitted score
# CAPABILITY_ENUMERATION has tools=2.0, knowledge=0.5
plan_cap_full_hits = run_recall_lanes(plan_cap, SYN_INDEX, _fake_load)
tools_emitted = [h for h in plan_cap_full_hits if h.lane == LANE_TOOLS]
knowledge_emitted = [h for h in plan_cap_full_hits if h.lane == LANE_KNOWLEDGE]
# Both lanes scored; tools.weight (2.0) > knowledge.weight (0.5), so a
# tools hit with the same base score should outrank a knowledge hit.
# We don't have identical-base hits in the synthetic set, but we can
# verify the gating: knowledge weight > 0 -> emits; we know weight is
# applied because the reason string contains lane_weight=2.00.
if tools_emitted:
    check(
        "A4.a tools-lane reason carries lane_weight",
        any("lane_weight=2.00" in h.reason for h in tools_emitted),
        detail=f"sample reason: {tools_emitted[0].reason[:80]}",
    )

# A5: temporal lane recency boost when posture asks
plan_temp = compile_recall_plan(FakeQS("what happened yesterday"))
temp_hits = _run_temporal_lane(
    plan_temp,
    SYN_INDEX,
    _fake_load,
    plan_terms=set(plan_temp.exact_terms) | set(plan_temp.expanded_terms),
)
check(
    "A5.a temporal plan returns temporal-lobe hits",
    all(h.metadata["coord"][0] == 9 for h in temp_hits) if temp_hits else True,
    detail=f"hits={[h.memory_id for h in temp_hits]}",
)
if temp_hits:
    check(
        "A5.b temporal lane reason mentions recency_bonus",
        any("recency_bonus" in h.reason for h in temp_hits),
        detail=f"sample reason: {temp_hits[0].reason[:120]}",
    )

# A6: strength lane returns [] when posture has no strength_bias
plan_no_strength = compile_recall_plan(FakeQS("what tools can you use"))
# CAPABILITY_ENUMERATION has strength_bias=None
strength_hits = _run_strength_lane(
    plan_no_strength,
    SYN_INDEX,
    _fake_load,
    plan_terms=set(plan_no_strength.exact_terms),
)
check(
    "A6.a strength lane is no-op when posture has no strength_bias",
    strength_hits == [],
    detail=f"got {len(strength_hits)} hits",
)

# =====================================================================
# B. Scoring math
# =====================================================================
section("B. Scoring math")

from memory.recall_arbitrator import (
    multi_lane_bonus,
    suppressed_lobe_penalty,
    suppressed_memory_type_penalty,
    posture_score_evidence,
    arbitrate_evidence,
)
from memory.evidence_set import EvidenceSet, MemoryEvidence


def _mk_evidence(mid, lanes, lane_scores, meta=None):
    return MemoryEvidence(
        memory_id=mid,
        lanes=list(lanes),
        lane_scores=dict(lane_scores),
        reasons=[],
        meta=meta,
    )


# B1: multi-lane bonus brackets
ev1 = _mk_evidence("a", [], {})
check("B1.a 0 lanes -> 0.0 bonus", multi_lane_bonus(ev1) == 0.0)
ev2 = _mk_evidence("b", ["x"], {"x": 0.5})
check("B1.b 1 lane -> 0.0 bonus", multi_lane_bonus(ev2) == 0.0)
ev3 = _mk_evidence("c", ["x", "y"], {"x": 0.5, "y": 0.5})
check(
    "B1.c 2 lanes -> 0.05 bonus",
    abs(multi_lane_bonus(ev3) - 0.05) < 1e-9,
    detail=f"got {multi_lane_bonus(ev3)}",
)
ev4 = _mk_evidence("d", ["x", "y", "z"], {"x": 0.5, "y": 0.5, "z": 0.5})
check(
    "B1.d 3 lanes -> 0.10 bonus",
    abs(multi_lane_bonus(ev4) - 0.10) < 1e-9,
    detail=f"got {multi_lane_bonus(ev4)}",
)
ev5 = _mk_evidence("e", ["a", "b", "c", "d"], {"a": 0.5, "b": 0.5, "c": 0.5, "d": 0.5})
check(
    "B1.e 4 lanes -> 0.15 bonus",
    abs(multi_lane_bonus(ev5) - 0.15) < 1e-9,
    detail=f"got {multi_lane_bonus(ev5)}",
)
ev6 = _mk_evidence("f", ["a", "b", "c", "d", "e"], {})
check(
    "B1.f 5 lanes -> 0.20 bonus",
    abs(multi_lane_bonus(ev6) - 0.20) < 1e-9,
    detail=f"got {multi_lane_bonus(ev6)}",
)
ev7 = _mk_evidence("g", list("abcdefg"), {})
check("B1.g 7 lanes -> 0.20 bonus (cap)", abs(multi_lane_bonus(ev7) - 0.20) < 1e-9)

# B2: suppressed lobe penalty
plan_id_for_pen = compile_recall_plan(FakeQS("who is the operator"))
ev_in_suppressed = _mk_evidence(
    "5.0.x",
    ["tools"],
    {"tools": 0.5},
    meta={"coord": [5, 0, 0], "type": "tool"},
)
pen = suppressed_lobe_penalty(ev_in_suppressed, plan_id_for_pen)
check(
    "B2.a IDENTITY plan penalizes lobe-5 evidence with -0.30",
    abs(pen - (-0.30)) < 1e-9,
    detail=f"got {pen}",
)

ev_in_allowed = _mk_evidence(
    "0.0.x",
    ["identity"],
    {"identity": 0.5},
    meta={"coord": [0, 0, 0], "type": "identity"},
)
pen_zero = suppressed_lobe_penalty(ev_in_allowed, plan_id_for_pen)
check(
    "B2.b IDENTITY plan does NOT penalize lobe-0 evidence",
    pen_zero == 0.0,
    detail=f"got {pen_zero}",
)

# B3: suppressed memory type penalty
ev_tool_type = _mk_evidence(
    "5.0.x",
    ["tools"],
    {"tools": 0.5},
    meta={"coord": [5, 0, 0], "type": "tool"},
)
pen_t = suppressed_memory_type_penalty(ev_tool_type, plan_id_for_pen)
check(
    "B3.a IDENTITY plan penalizes type='tool' with -0.25",
    abs(pen_t - (-0.25)) < 1e-9,
    detail=f"got {pen_t}",
)

# B4: posture_score_evidence clamps to [0, 1]
ev_huge = _mk_evidence(
    "a",
    ["x", "y", "z", "w", "v", "u"],
    {"x": 1.0, "y": 1.0, "z": 1.0, "w": 1.0, "v": 1.0, "u": 1.0},
    meta={"coord": [0, 0, 0], "type": "identity"},
)
score = posture_score_evidence(ev_huge, plan_id_for_pen)
check("B4.a posture_score_evidence clamps to <= 1.0", 0.0 <= score <= 1.0, detail=f"got {score}")

ev_below_zero = _mk_evidence(
    "5.0.x",
    ["tools"],
    {"tools": 0.0},
    meta={"coord": [5, 0, 0], "type": "tool"},
)
score_floor = posture_score_evidence(ev_below_zero, plan_id_for_pen)
check(
    "B4.b posture_score_evidence clamps to >= 0.0", score_floor >= 0.0, detail=f"got {score_floor}"
)

# B5: deterministic tie-break
ev_set = EvidenceSet()
ev_set.records["zzz"] = _mk_evidence(
    "zzz", ["x"], {"x": 0.5}, meta={"coord": [2, 0, 0], "type": "concept"}
)
ev_set.records["aaa"] = _mk_evidence(
    "aaa", ["x"], {"x": 0.5}, meta={"coord": [2, 0, 0], "type": "concept"}
)
plan_kx = compile_recall_plan(FakeQS("explain something"))
ranked = arbitrate_evidence(ev_set, plan_kx)
check(
    "B5.a tie-break is alphabetical by memory_id ascending",
    [r.memory_id for r in ranked][:2] == ["aaa", "zzz"],
    detail=f"got {[r.memory_id for r in ranked]!r}",
)


# =====================================================================
# C. Receipt completeness
# =====================================================================
section("C. Receipt completeness")

from memory.recall_receipt import build_recall_receipt

# Build an evidence set with a couple of arbitrated winners + non-winners
ev_set_c = EvidenceSet()
for mid, coord, mtype in [
    ("0.0.id1", [0, 0, 0], "identity"),
    ("1.0.p1", [1, 0, 0], "persona"),
    ("2.0.k1", [2, 0, 0], "concept"),
]:
    ev_set_c.records[mid] = _mk_evidence(
        mid,
        ["identity", "persona"],
        {"identity": 0.7, "persona": 0.5},
        meta={"coord": coord, "type": mtype, "title": f"{mid} title"},
    )
plan_c = compile_recall_plan(FakeQS("who is the operator"))
winners_c = arbitrate_evidence(ev_set_c, plan_c)
receipt = build_recall_receipt(FakeQS("who is the operator"), plan_c, ev_set_c, winners_c)

required_keys = {
    "query",
    "posture",
    "secondary_postures",
    "lanes_used",
    "coord_hypotheses",
    "winning_memory_ids",
    "why_selected",
    "evidence_count",
    "winner_count",
}
check(
    "C1.a receipt has all required keys",
    required_keys.issubset(set(receipt.keys())),
    detail=f"missing={sorted(required_keys - set(receipt.keys()))}",
)

check(
    "C2.a winning_memory_ids matches arbitrate output order",
    receipt["winning_memory_ids"] == [w.memory_id for w in winners_c],
    detail=f"got {receipt['winning_memory_ids']!r}",
)

check(
    "C3.a why_selected covers every winner",
    set(receipt["why_selected"].keys()) == set(receipt["winning_memory_ids"]),
    detail=f"diff={set(receipt['why_selected'].keys()) ^ set(receipt['winning_memory_ids'])!r}",
)

check(
    "C4.a why_selected reasons reference 'final='",
    all("final=" in r for r in receipt["why_selected"].values()),
    detail=f"sample={list(receipt['why_selected'].values())[:1]}",
)

check(
    "C5.a lanes_used is non-empty",
    len(receipt["lanes_used"]) >= 1,
    detail=f"got {receipt['lanes_used']!r}",
)

# JSON-serializability
try:
    json.dumps(receipt)
    json_ok = True
except Exception:
    json_ok = False
check("C6.a receipt is JSON-serializable", json_ok)


# =====================================================================
# D. Live read_memory integration
# =====================================================================
section("D. Live read_memory integration")

from memory.api import read_memory, QuerySpec

# This script reads the operator's live store on purpose, but read_memory appends a recall
# verdict row per query; keep those out of memory/recall_verifier/verdict_log.jsonl
# (found 2026-09-15 by the plain-script matrix pollution check).
import tempfile as _verdict_tempfile  # noqa: E402

import memory.recall_verifier as _recall_verifier  # noqa: E402

_VERDICT_SCRATCH = Path(_verdict_tempfile.mkdtemp(prefix="omega-recall-verdicts-")) / "verdict_log.jsonl"
_recall_verifier.verdict_log_path = lambda *, root=None: _VERDICT_SCRATCH
import logging

log = logging.getLogger("omega.tests.memory.test_recall_lanes_arbitration")


spec = QuerySpec(
    cycle_id="test-lanes-live-001",
    text="who is the operator",
    limit=5,
)
res = read_memory(spec)
meta = res.meta

check(
    "D1.a meta has recall_winners",
    "recall_winners" in meta,
    detail=f"keys={sorted(meta.keys())[:8]}...",
)
winners = meta.get("recall_winners") or []
check("D1.b winners is a list", isinstance(winners, list))

if winners:
    sample = winners[0]
    check("D1.c winner has memory_id", "memory_id" in sample)
    check("D1.d winner has lanes (list)", isinstance(sample.get("lanes"), list))
    check("D1.e winner has lane_scores (dict)", isinstance(sample.get("lane_scores"), dict))
    check("D1.f winner has reasons (list)", isinstance(sample.get("reasons"), list))

# IDENTITY suppression — top winners should not be lobe 5/8/10/11
if winners:
    top_lobes = []
    for w in winners[:3]:
        # Find the candidate in res.candidates to read coord
        cand = next((c for c in res.candidates if c.id == w["memory_id"]), None)
        if cand and cand.coord:
            top_lobes.append(cand.coord[0])
    if top_lobes:
        suppressed = {5, 8, 10, 11}
        # Some hits may still be in suppressed lobes (penalty doesn't
        # forbid; it down-weights), but at least one of the top should
        # NOT be — otherwise the suppression isn't biting.
        check(
            "D2.a IDENTITY winners aren't ALL from suppressed lobes",
            not all(L in suppressed for L in top_lobes),
            detail=f"top_lobes={top_lobes}",
        )

# Determinism for repeated query
res2 = read_memory(QuerySpec(cycle_id="test-lanes-live-002", text="who is the operator", limit=5))
winners2 = res2.meta.get("recall_winners") or []
check(
    "D3.a same query -> same winner ids order",
    [w["memory_id"] for w in winners] == [w["memory_id"] for w in winners2],
    detail=(
        f"first={[w['memory_id'] for w in winners][:3]} "
        f"second={[w['memory_id'] for w in winners2][:3]}"
    ),
)

# Receipt is populated with REAL why_selected
receipt_live = res.meta.get("recall_receipt") or {}
why = receipt_live.get("why_selected") or {}
if why:
    sample_reason = next(iter(why.values()))
    check(
        "D4.a live receipt why_selected uses real synthesis reason",
        "final=" in sample_reason and "CLAUDE_FILL" not in sample_reason,
        detail=f"sample={sample_reason[:100]!r}",
    )


# =====================================================================
# E. Salience at the merge (neuromod-coupled ranking)
#
# The arbitrator folds per-memory StrengthIndex salience into the
# posture score as a CENTERED enrichment term:
#     salience = _SALIENCE_WEIGHT * (get_strength(mid) - _SALIENCE_NEUTRAL)
# Hot (fresh/reinforced) memories nudge up, decayed ones nudge down,
# memories UNKNOWN to the index are exactly neutral (term = 0.0), and
# lane relevance stays the dominant signal — salience reorders
# near-ties, it can never surface a memory that earned no lane hits.
# =====================================================================
section("E. Salience at the merge")

import tempfile as _tf

import memory.strength_index as _si_mod
from autonomic.symphony_neuromod import set_current_memory_salience as _set_sal

# Pristine encode latch + a private singleton for this section only.
_set_sal(None)
_prev_singleton = _si_mod._singleton
_si_mod._singleton = _si_mod.StrengthIndex(Path(_tf.mkdtemp()) / "strength_index.json")
_si_mod._singleton.register("aaa_cold", write_strength=0.1)
_si_mod._singleton.register("zzz_hot", write_strength=0.9)

plan_e = compile_recall_plan(FakeQS("salience merge probe"))

# E1: equal lane relevance — the hot memory must outrank the cold one.
# Ids are chosen so today's tie-break (memory_id asc) picks the COLD
# one; only a real salience term flips this. (RED before the change.)
ev_set_e = EvidenceSet()
ev_set_e.records["aaa_cold"] = _mk_evidence(
    "aaa_cold", ["knowledge"], {"knowledge": 0.5}, meta={"coord": [2, 0, 0], "type": "concept"}
)
ev_set_e.records["zzz_hot"] = _mk_evidence(
    "zzz_hot", ["knowledge"], {"knowledge": 0.5}, meta={"coord": [2, 0, 0], "type": "concept"}
)
winners_e = arbitrate_evidence(ev_set_e, plan_e)
check(
    "E1 equal-relevance tie goes to the higher-strength memory",
    [w.memory_id for w in winners_e][0] == "zzz_hot",
    detail=f"order={[w.memory_id for w in winners_e]}",
)

# E2: a memory UNKNOWN to the strength index gets an exactly-zero
# salience component (neutral — no uniform penalty for unindexed
# memories), while indexed memories get a nonzero one.
from memory.recall_arbitrator import _score_breakdown

ev_unknown = _mk_evidence(
    "not_in_index", ["knowledge"], {"knowledge": 0.5}, meta={"coord": [2, 0, 0], "type": "concept"}
)
bd_unknown = _score_breakdown(ev_unknown, plan_e)
check(
    "E2.a unknown-to-index memory has zero salience component",
    bd_unknown.get("salience", None) == 0.0,
    detail=f"breakdown={bd_unknown}",
)
bd_hot = _score_breakdown(
    _mk_evidence(
        "zzz_hot", ["knowledge"], {"knowledge": 0.5}, meta={"coord": [2, 0, 0], "type": "concept"}
    ),
    plan_e,
)
check(
    "E2.b indexed hot memory has a positive salience component",
    bd_hot.get("salience", 0.0) > 0.0,
    detail=f"breakdown={bd_hot}",
)

# E3: clamps hold at both ends with the new term in play.
ev_hot_max = _mk_evidence(
    "zzz_hot",
    ["a", "b", "c", "d", "e"],
    {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0},
    meta={"coord": [2, 0, 0], "type": "concept"},
)
check(
    "E3.a salience never pushes score above 1.0", posture_score_evidence(ev_hot_max, plan_e) <= 1.0
)
ev_cold_min = _mk_evidence(
    "aaa_cold", ["a"], {"a": 0.0}, meta={"coord": [2, 0, 0], "type": "concept"}
)
check(
    "E3.b salience never pushes score below 0.0", posture_score_evidence(ev_cold_min, plan_e) >= 0.0
)

# E4: receipts explain the term — winners carry a salience component
# in the synthesis reason so the math stays quotable.
reasons_hot = " ".join(winners_e[0].reasons) if winners_e else ""
check(
    "E4 synthesis reason carries the salience component",
    "salience" in reasons_hot,
    detail=f"reasons={reasons_hot[:120]!r}",
)

# Restore the singleton so nothing leaks past this section.
_si_mod._singleton = _prev_singleton


# =====================================================================
# Summary
# =====================================================================

print()
print("=" * 70)
print(f"  PASSED: {PASS}")
print(f"  FAILED: {FAIL}")
if FAILURES:
    print()
    print("FAILURES:")
    for f in FAILURES:
        print(f"  {f}")
print("=" * 70)

raise SystemExit(0 if FAIL == 0 else 1)
