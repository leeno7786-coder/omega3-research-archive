#!/usr/bin/env python3
"""
test_recall_posture_stress.py — adversarial / break-it suite
==============================================================

This is the "try to break it" pass after the Recall Posture stack
landed clean across the focused suites + Copilot's 3-iteration
swarm audit. Mindset: hostile. We're hunting for crashes, lies,
non-determinism, posture mis-routing, and scoring math drift.

Five attack angles:

    A. Adversarial queries
       Empty / whitespace-only / unicode-only / very long / injection-
       flavor / multi-pattern conflict. Each one should produce a valid
       result (or graceful empty), never an exception.

    B. Determinism under StrengthIndex reconsolidation
       read_memory `touches` strength on every hit. Repeated identical
       queries should produce stable winner ordering even as strength
       scores shift between calls. Detect: ranking drift across runs.

    C. Score math sanity
       For every winner, the receipt's `final=X.YYY` reason has to
       arithmetically match `base + multi + ans + lobe_pen + type_pen`
       (allowing for clamp). Detect: receipt lying about the math.

    D. Suppression actually de-ranks
       For an IDENTITY query: confirm that lobe-5 / lobe-8 / lobe-10 /
       lobe-11 hits (suppressed lobes per posture) score lower than
       allowed-lobe peers via per-evidence scoring. Pure unit test
       against the arbitrator with constructed evidence.

    E. Concurrent recall thread safety
       8 threads × 5 queries each. No exceptions, no malformed results,
       no shared-state corruption.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
WARN = 0
FAILURES: list[str] = []
WARNINGS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{label}  {detail}")


def warn(label: str, detail: str = "") -> None:
    """Soft signal — non-blocking observation we want logged but not
    treated as a defect."""
    global WARN
    WARN += 1
    msg = f"  [WARN] {label}" + (f"  ({detail})" if detail else "")
    print(msg)
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


def _safe_recall(text: str, cycle_id: str = "stress-test", limit: int = 5):
    """Wrapper that captures any exception as a structured result."""
    try:
        spec = QuerySpec(cycle_id=cycle_id, text=text, limit=limit)
        res = read_memory(spec)
        return ("ok", res, None)
    except Exception as exc:
        return ("exception", None, f"{type(exc).__name__}: {exc}")


# =====================================================================
# A. Adversarial queries
# =====================================================================
section("A. Adversarial queries — should never crash, should degrade clean")

ADVERSARIAL_BATTERY = [
    # (label, query, expected_minimum_behavior)
    ("A1.a empty string", ""),
    ("A1.b whitespace only", "   \t\n   "),
    ("A1.c single character", "?"),
    ("A1.d single word", "operator"),
    ("A1.e ASCII null bytes", "\x00\x00\x00"),
    ("A1.f unicode-only", "你好世界 здравствуй"),
    ("A1.g emoji-only", "🤖🧠💾"),
    ("A1.h sql-injection-flavor", "'; DROP TABLE memory; --"),
    ("A1.i regex-meta-flavor", "[.*+?^${}()|]"),
    ("A1.j very long (10K chars)", "operator " * 1250),
    ("A1.k multi-posture conflict", "remind me what tools you have for the python work"),
    ("A1.l negation", "what tools can you not use"),
    ("A1.m all-caps", "WHO IS THE OPERATOR"),
    ("A1.n typos", "wuts photosynthsis"),
    ("A1.o capitalized misleads", "Yesterday Operator asked about Python"),
    ("A1.p question-as-statement", "tell me who the operator is"),
    ("A1.q nested punctuation", "(((operator)))"),
    ("A1.r mixed scripts", "operator 操作员 опера́тор"),
    ("A1.s URL-form (bypass)", "https://example.com/some/path"),
    ("A1.t nothing-but-stopwords", "the and of to with for"),
]

for label, q in ADVERSARIAL_BATTERY:
    status, res, err = _safe_recall(q, cycle_id=f"stress-{abs(hash(label)) % 99999}")
    if status == "exception":
        check(f"{label} no exception", False, detail=err)
        continue
    check(f"{label} no exception", True)
    # Result must be well-formed regardless of content
    well_formed = (
        res is not None
        and isinstance(res.candidates, list)
        and isinstance(res.meta, dict)
        and isinstance(res.meta.get("posture"), str)
        and isinstance(res.meta.get("recall_plan"), dict)
        and isinstance(res.meta.get("recall_receipt"), dict)
    )
    check(
        f"{label} well-formed result",
        well_formed,
        detail=f"posture={res.meta.get('posture')!r} cands={len(res.candidates)}",
    )


# =====================================================================
# B. Determinism under reconsolidation side effect
# =====================================================================
section("B. Determinism — repeated query, ranking should stay stable")

# read_memory touches StrengthIndex on every hit. If the next recall's
# arbitration uses strength signal, the touch should NOT cascade into
# observable winner-order drift across consecutive calls. Verify by
# running the same query 5x in a row and comparing winner_id order.

DET_QUERIES = [
    "who is the operator",
    "what tools can you use",
    "what do you know about photosynthesis",
]

for q in DET_QUERIES:
    orders = []
    for i in range(5):
        spec = QuerySpec(cycle_id=f"det-{abs(hash(q)) % 99999}-{i}", text=q, limit=5)
        res = read_memory(spec)
        winners = res.meta.get("recall_winners") or []
        orders.append([w["memory_id"] for w in winners])

    distinct = {tuple(o) for o in orders}
    label = f"B1 same-query stability: {q!r:50s}"
    if len(distinct) == 1:
        check(label, True, detail=f"5x identical: {orders[0][:3]!r}")
    else:
        # Drift is a real concern but may not be a defect if the
        # difference is at the score-tie boundary. Surface as WARN
        # with the diff so we can triage.
        warn(
            label,
            detail=(
                f"observed {len(distinct)} distinct orders across 5 runs — "
                f"first={orders[0][:3]} last={orders[-1][:3]}"
            ),
        )


# =====================================================================
# C. Score math sanity — receipt synthesis reason must arithmetic-check
# =====================================================================
section("C. Score math — does receipt's final = sum of components (after clamp)?")

# Receipt format: "final=X.YYY = base_lane(A) + multi_lane(B) + answer(C) + lobe_pen(D) + type_pen(E)"
_REASON_RE = re.compile(
    r"final=([\d.]+)\s*=\s*"
    r"base_lane\(([\d.\-]+)\)\s*\+\s*"
    r"multi_lane\(([\d.\-]+)\)\s*\+\s*"
    r"answer\(([\d.\-]+)\)\s*\+\s*"
    r"lobe_pen\(([\d.\-]+)\)\s*\+\s*"
    r"type_pen\(([\d.\-]+)\)"
)


def _parse_reason(reason: str):
    m = _REASON_RE.search(reason)
    if not m:
        return None
    return [float(x) for x in m.groups()]


math_queries = [
    "who is the operator",
    "what tools can you use",
    "what do you know about photosynthesis",
]
math_total_winners = 0
math_arith_mismatches = 0
for q in math_queries:
    spec = QuerySpec(cycle_id=f"math-{abs(hash(q)) % 99999}", text=q, limit=5)
    res = read_memory(spec)
    receipt = res.meta.get("recall_receipt") or {}
    why = receipt.get("why_selected") or {}
    for mid, reason in why.items():
        parsed = _parse_reason(reason)
        if parsed is None:
            continue
        final, base, multi, answer, lobe_pen, type_pen = parsed
        raw_sum = base + multi + answer + lobe_pen + type_pen
        # The arbitrator clamps to [0,1] AFTER summing. So expect:
        #   final ~~ clamp(raw_sum, 0, 1)
        expected_final = max(0.0, min(1.0, raw_sum))
        diff = abs(final - expected_final)
        math_total_winners += 1
        if diff > 0.01:
            math_arith_mismatches += 1

check(
    "C1 receipts arithmetic-check (final ~~ clamp(sum_components))",
    math_arith_mismatches == 0,
    detail=f"checked={math_total_winners} mismatches={math_arith_mismatches}",
)


# =====================================================================
# D. Suppression actually de-ranks (unit test against arbitrator)
# =====================================================================
section("D. Suppression — IDENTITY plan must rank lobe-0/1 above lobe-5/8/10/11")

# Construct synthetic evidence with IDENTICAL lane scores across two
# memories — one in an allowed lobe (lobe 0), one in a suppressed lobe
# (lobe 5). With everything else equal, the suppression penalty should
# flip the ranking.

from memory.recall_compiler import compile_recall_plan
from memory.recall_arbitrator import (
    arbitrate_evidence,
    posture_score_evidence,
)
from memory.evidence_set import EvidenceSet, MemoryEvidence
import logging

log = logging.getLogger("omega.tests.memory.test_recall_posture_stress")


class FakeQS:
    def __init__(self, text: str, limit: int = 10):
        self.cycle_id = "suppression-test"
        self.text = text
        self.limit = limit
        self.modality = "text"
        self.task_scope = None
        self.recall_policy_id = None
        self.domain = None


plan_id = compile_recall_plan(FakeQS("who is the operator"))

ev_set_d = EvidenceSet()
# Allowed lobe: identity (lobe 0) — IDENTITY plan should NOT penalize
ev_allowed = MemoryEvidence(
    memory_id="0.0.aaa",
    lanes=["identity", "persona"],
    lane_scores={"identity": 0.5, "persona": 0.5},
    reasons=[],
    meta={
        "coord": [0, 0, 0],
        "type": "identity",
        "title": "operator identity record",
        "summary": "the operator",
    },
)
ev_set_d.records["0.0.aaa"] = ev_allowed

# Suppressed lobe: skills (lobe 5) — IDENTITY plan suppresses lobe 5
# AND tool type. Same lane scores; suppression should flip rank.
ev_suppressed = MemoryEvidence(
    memory_id="5.0.bbb",
    lanes=["tools", "skills"],
    lane_scores={"tools": 0.5, "skills": 0.5},
    reasons=[],
    meta={
        "coord": [5, 0, 0],
        "type": "tool",
        "title": "operator",
        "summary": "tool that mentions operator",
    },
)
ev_set_d.records["5.0.bbb"] = ev_suppressed

score_allowed = posture_score_evidence(ev_allowed, plan_id)
score_suppressed = posture_score_evidence(ev_suppressed, plan_id)

check(
    "D1.a allowed-lobe scores higher than suppressed-lobe (same lane scores)",
    score_allowed > score_suppressed,
    detail=f"allowed={score_allowed:.3f} suppressed={score_suppressed:.3f}",
)

ranked = arbitrate_evidence(ev_set_d, plan_id)
check(
    "D1.b arbitrate returns allowed-lobe first",
    ranked and ranked[0].memory_id == "0.0.aaa",
    detail=f"order={[r.memory_id for r in ranked]!r}",
)

# Reverse polarity: CAPABILITY plan (suppresses identity, prefers tools)
plan_cap = compile_recall_plan(FakeQS("what tools can you use"))
ev_set_e = EvidenceSet()
ev_set_e.records["0.0.aaa"] = MemoryEvidence(
    memory_id="0.0.aaa",
    lanes=["identity"],
    lane_scores={"identity": 0.5},
    reasons=[],
    meta={"coord": [0, 0, 0], "type": "identity", "title": "operator identity record"},
)
ev_set_e.records["5.0.bbb"] = MemoryEvidence(
    memory_id="5.0.bbb",
    lanes=["tools"],
    lane_scores={"tools": 0.5},
    reasons=[],
    meta={"coord": [5, 0, 0], "type": "tool", "title": "web_fetch", "summary": "fetch web content"},
)

ranked_cap = arbitrate_evidence(ev_set_e, plan_cap)
check(
    "D2.a CAPABILITY plan ranks tool-lobe above identity",
    ranked_cap and ranked_cap[0].memory_id == "5.0.bbb",
    detail=f"order={[r.memory_id for r in ranked_cap]!r}",
)


# =====================================================================
# E. Concurrent recall — 8 threads × 5 queries
# =====================================================================
section("E. Concurrent recall — thread safety")

CONCURRENT_QUERIES = [
    "who is the operator",
    "what tools can you use",
    "what do you know about photosynthesis",
    "remind me about the python work",
    "operator",
]

results_lock = threading.Lock()
thread_results: list = []
thread_errors: list = []


def _worker(thread_id: int):
    for i, q in enumerate(CONCURRENT_QUERIES):
        try:
            spec = QuerySpec(cycle_id=f"thread-{thread_id}-q{i}", text=q, limit=5)
            res = read_memory(spec)
            with results_lock:
                thread_results.append(
                    {
                        "thread": thread_id,
                        "query": q,
                        "posture": res.meta.get("posture"),
                        "candidates": len(res.candidates),
                        "winners": [w["memory_id"] for w in (res.meta.get("recall_winners") or [])],
                    }
                )
        except Exception as exc:
            with results_lock:
                thread_errors.append((thread_id, q, f"{type(exc).__name__}: {exc}"))


N_THREADS = 8
threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N_THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=120.0)

# Were any threads still alive at timeout?
stuck = [i for i, t in enumerate(threads) if t.is_alive()]
check("E1.a no threads timed out", not stuck, detail=f"stuck={stuck!r}" if stuck else "")

# Did any thread raise?
check(
    "E1.b no exceptions across all worker threads",
    not thread_errors,
    detail=f"errors={thread_errors[:3]!r}" if thread_errors else "",
)

# All expected results present?
expected_count = N_THREADS * len(CONCURRENT_QUERIES)
check(
    "E1.c all expected concurrent results returned",
    len(thread_results) == expected_count,
    detail=f"got={len(thread_results)} expected={expected_count}",
)

# All concurrent runs of the same query produce the same posture?
posture_by_query = {}
for r in thread_results:
    posture_by_query.setdefault(r["query"], set()).add(r["posture"])
posture_drift = {q: postures for q, postures in posture_by_query.items() if len(postures) > 1}
check(
    "E1.d same query produces same posture across all threads",
    not posture_drift,
    detail=f"drift={posture_drift!r}" if posture_drift else "",
)


# =====================================================================
# Summary
# =====================================================================
print()
print("=" * 70)
print(f"  PASSED:   {PASS}")
print(f"  FAILED:   {FAIL}")
print(f"  WARNINGS: {WARN}")
if FAILURES:
    print()
    print("FAILURES:")
    for f in FAILURES:
        print(f"  {f}")
if WARNINGS:
    print()
    print("WARNINGS:")
    for w in WARNINGS:
        print(f"  {w}")
print("=" * 70)

# Warnings don't fail the suite — they're triage signal.
raise SystemExit(0 if FAIL == 0 else 1)
