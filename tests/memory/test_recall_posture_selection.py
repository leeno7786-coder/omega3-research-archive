#!/usr/bin/env python3
"""
test_recall_posture_selection.py
Regression tests for deterministic recall posture selection.
"""

from __future__ import annotations

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


from memory.recall_posture import (
    POSTURE_CATALOG,
    PostureDecision,
    RecallPosture,
    select_recall_posture,
)
import logging

log = logging.getLogger("omega.tests.memory.test_recall_posture_selection")


# =====================================================================
# A. Catalog integrity
# =====================================================================
section("A. Posture catalog integrity")

EXPECTED_POSTURES = [
    "IDENTITY_RESOLUTION",
    "ROLE_BINDING",
    "BACKGROUND_LOOKUP",
    "TASK_CONTINUITY",
    "REMINDER",
    "CAPABILITY_ENUMERATION",
    "KNOWLEDGE_EXPLANATION",
    "PROCEDURAL_HOW",
    "ARCHITECTURAL_WHY",
    "TEMPORAL_RECALL",
    "DECISION_RECALL",
    "ERROR_RECALL",
    "PROJECT_CONTEXT",
    "SYSTEM_STATUS",
]

for name in EXPECTED_POSTURES:
    check(f"A1.a catalog has {name}", name in POSTURE_CATALOG)

check("A2 catalog size is 14", len(POSTURE_CATALOG) == 14)

# Spot-check preferred/suppressed lobes
id_posture = POSTURE_CATALOG["IDENTITY_RESOLUTION"]
check("A3 IDENTITY preferred_lobes includes 0", 0 in id_posture.preferred_lobes)
check("A4 IDENTITY suppressed_lobes includes 5", 5 in id_posture.suppressed_lobes)
check("A5 IDENTITY lane_weights['tools'] == 0.0", id_posture.lane_weights.get("tools") == 0.0)

cap_posture = POSTURE_CATALOG["CAPABILITY_ENUMERATION"]
check("A6 CAPABILITY preferred_lobes includes 5", 5 in cap_posture.preferred_lobes)
check("A7 CAPABILITY lane_weights['tools'] == 2.0", cap_posture.lane_weights.get("tools") == 2.0)


# =====================================================================
# B. Deterministic selection — known regression queries
# =====================================================================
section("B. Known regression query selection")

# B1. who is the operator -> IDENTITY_RESOLUTION
r = select_recall_posture("who is the operator")
check(
    "B1.a primary posture is IDENTITY_RESOLUTION",
    r.posture == "IDENTITY_RESOLUTION",
    detail=f"got {r.posture}",
)
check("B1.b confidence >= 0.45", r.confidence >= 0.45, detail=f"conf={r.confidence:.2f}")
check(
    "B1.c ROLE_BINDING in secondary",
    "ROLE_BINDING" in r.secondary_postures,
    detail=f"secondaries={r.secondary_postures}",
)

# B2. remind me about the python work -> TASK_CONTINUITY
r2 = select_recall_posture("remind me about the python work")
check(
    "B2.a primary posture is TASK_CONTINUITY",
    r2.posture == "TASK_CONTINUITY",
    detail=f"got {r2.posture}",
)
check("B2.b confidence >= 0.45", r2.confidence >= 0.45, detail=f"conf={r2.confidence:.2f}")

# B3. what tools can you use -> CAPABILITY_ENUMERATION
r3 = select_recall_posture("what tools can you use")
check(
    "B3.a primary posture is CAPABILITY_ENUMERATION",
    r3.posture == "CAPABILITY_ENUMERATION",
    detail=f"got {r3.posture}",
)
check("B3.b confidence >= 0.45", r3.confidence >= 0.45, detail=f"conf={r3.confidence:.2f}")

# B4. what do you know about photosynthesis -> KNOWLEDGE_EXPLANATION
r4 = select_recall_posture("what do you know about photosynthesis")
check(
    "B4.a primary posture is KNOWLEDGE_EXPLANATION",
    r4.posture == "KNOWLEDGE_EXPLANATION",
    detail=f"got {r4.posture}",
)
check("B4.b confidence >= 0.45", r4.confidence >= 0.45, detail=f"conf={r4.confidence:.2f}")

# B5. operator -> fallback/multi-posture (low confidence)
r5 = select_recall_posture("operator")
check("B5.a fallback_used is True", r5.fallback_used is True)
check("B5.b posture is MULTI_POSTURE or low-confidence", r5.posture == "MULTI_POSTURE")
check(
    "B5.c secondary_postures non-empty",
    len(r5.secondary_postures) >= 2,
    detail=f"secondaries={r5.secondary_postures}",
)


# =====================================================================
# C. Edge cases
# =====================================================================
section("C. Edge cases")

empty = select_recall_posture("")
check("C1 empty query returns fallback", empty.fallback_used is True)
check("C2 empty query posture is BACKGROUND_LOOKUP", empty.posture == "BACKGROUND_LOOKUP")

none_query = select_recall_posture(None)  # type: ignore[arg-type]
check("C3 None query handled gracefully", none_query.fallback_used is True)


# =====================================================================
# D. Determinism
# =====================================================================
section("D. Determinism")

r_a = select_recall_posture("who is the operator")
r_b = select_recall_posture("who is the operator")
check("D1 same query -> same posture", r_a.posture == r_b.posture)
check("D2 same query -> same confidence", r_a.confidence == r_b.confidence)
check("D3 same query -> same secondaries", r_a.secondary_postures == r_b.secondary_postures)


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
