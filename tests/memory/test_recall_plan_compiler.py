#!/usr/bin/env python3
"""
test_recall_plan_compiler.py
Regression tests for RecallPlan compilation.
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


from memory.recall_compiler import compile_recall_plan
from memory.recall_posture import POSTURE_CATALOG
import logging

log = logging.getLogger("omega.tests.memory.test_recall_plan_compiler")


# Duck-typed QuerySpec for tests (avoids importing memory.api at test time)
class FakeQuerySpec:
    def __init__(
        self, text: str, limit: int = 10, domain: str | None = None, task_scope: str | None = None
    ):
        self.cycle_id = "test-cycle"
        self.text = text
        self.limit = limit
        self.domain = domain
        self.task_scope = task_scope
        self.modality = "text"
        self.recall_policy_id = None


# =====================================================================
# A. Plan shape validation
# =====================================================================
section("A. Plan shape validation")

spec = FakeQuerySpec("who is the operator", limit=10)
plan = compile_recall_plan(spec)

check("A1 plan.original_query preserved", plan.original_query == "who is the operator")
check("A2 plan.posture is RecallPosture", plan.posture.name == "IDENTITY_RESOLUTION")
check("A3 plan.intent set from posture", plan.intent == "identity")
check("A4 plan.query_type set from posture", plan.query_type == "self_description")
check("A5 plan.coord_hypotheses non-empty", len(plan.coord_hypotheses) >= 1)
check("A6 plan.exact_terms non-empty", len(plan.exact_terms) >= 2)
check("A7 plan.lane_weights is dict", isinstance(plan.lane_weights, dict))
check("A8 plan.limit_per_lane >= 2", plan.limit_per_lane >= 2)
check("A9 plan.final_limit >= 3", plan.final_limit >= 3)
check("A10 plan.min_strength is float", isinstance(plan.min_strength, float))


# =====================================================================
# B. Regression query plans
# =====================================================================
section("B. Regression query plans")

# B1 — identity
p1 = compile_recall_plan(FakeQuerySpec("who is the operator"))
check("B1.a posture is IDENTITY_RESOLUTION", p1.posture.name == "IDENTITY_RESOLUTION")
check("B1.b identity lane weight > 0", p1.lane_weights.get("identity", 0) > 0)
check("B1.c tools lane weight == 0", p1.lane_weights.get("tools", -1) == 0.0)
check("B1.d coord_hypotheses includes lobe 0", any(x == 0 for x, _, _ in p1.coord_hypotheses))

# B2 — task continuity
p2 = compile_recall_plan(FakeQuerySpec("remind me about the python work"))
check("B2.a posture is TASK_CONTINUITY", p2.posture.name == "TASK_CONTINUITY")
check("B2.b workflows lane weight > 0", p2.lane_weights.get("workflows", 0) > 0)
check("B2.c task_tags includes 'coding'", "coding" in p2.task_tags)

# B3 — capability
p3 = compile_recall_plan(FakeQuerySpec("what tools can you use"))
check("B3.a posture is CAPABILITY_ENUMERATION", p3.posture.name == "CAPABILITY_ENUMERATION")
check("B3.b tools lane weight == 2.0", p3.lane_weights.get("tools") == 2.0)
check("B3.c skills lane weight == 2.0", p3.lane_weights.get("skills") == 2.0)

# B4 — knowledge
p4 = compile_recall_plan(FakeQuerySpec("what do you know about photosynthesis"))
check("B4.a posture is KNOWLEDGE_EXPLANATION", p4.posture.name == "KNOWLEDGE_EXPLANATION")
check("B4.b knowledge lane weight == 2.0", p4.lane_weights.get("knowledge") == 2.0)
check("B4.c identity lane weight == 0", p4.lane_weights.get("identity") == 0.0)

# B5 — fallback / multi-posture
p5 = compile_recall_plan(FakeQuerySpec("operator"))
check("B5.a fallback triggers reduced budget", p5.limit_per_lane <= 5)
check("B5.b secondary_postures non-empty", len(p5.secondary_postures) >= 1)

# B6 — historical source validity must be explicit on the plan
p6_hist = compile_recall_plan(FakeQuerySpec("what did the project plan say in June"))
check("B6.a temporal query enables historical sources", p6_hist.include_historical_sources is True)
p6_current = compile_recall_plan(FakeQuerySpec("what is the current project plan"))
check(
    "B6.b current query keeps historical sources off",
    p6_current.include_historical_sources is False,
)


# =====================================================================
# C. Term extraction and entities
# =====================================================================
section("C. Terms and entities")

p6 = compile_recall_plan(FakeQuerySpec("what do you know about photosynthesis"))
check(
    "C1 exact_terms includes 'photosynthesis'", any("photosynthesis" in t for t in p6.exact_terms)
)
check("C2 expanded_terms non-empty", len(p6.expanded_terms) >= len(p6.exact_terms))

p7 = compile_recall_plan(FakeQuerySpec('who is "Dr. Smith"'))
check("C3 entities extracts quoted string", any("dr. smith" in e.lower() for e in p7.entities))


# =====================================================================
# D. Domain and task scope propagation
# =====================================================================
section("D. Domain and task scope")

p8 = compile_recall_plan(FakeQuerySpec("tool schema catalog", domain="tools"))
check(
    "D1 domain hint appears in expanded_terms or task_tags",
    "tools" in p8.task_tags or any("tools" in t for t in p8.expanded_terms),
)

p9 = compile_recall_plan(FakeQuerySpec("remind me about the python work", task_scope="coding"))
check("D2 task_scope appears in task_tags", "coding" in p9.task_tags)


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

if __name__ == "__main__":
    raise SystemExit(0 if FAIL == 0 else 1)

assert FAIL == 0, "\n".join(FAILURES)
