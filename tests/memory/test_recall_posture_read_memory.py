#!/usr/bin/env python3
"""
test_recall_posture_read_memory.py
Integration test: read_memory meta carries posture/plan/receipt.
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


from memory.api import read_memory, QuerySpec

# These recall scripts read the operator's live store on purpose, but read_memory appends a
# recall verdict row per query; keep those out of memory/recall_verifier/verdict_log.jsonl
# (found 2026-09-15 by the plain-script matrix pollution check).
import tempfile as _verdict_tempfile  # noqa: E402

import memory.recall_verifier as _recall_verifier  # noqa: E402

_VERDICT_SCRATCH = Path(_verdict_tempfile.mkdtemp(prefix="omega-recall-verdicts-")) / "verdict_log.jsonl"
_recall_verifier.verdict_log_path = lambda *, root=None: _VERDICT_SCRATCH
import logging

log = logging.getLogger("omega.tests.memory.test_recall_posture_read_memory")


# =====================================================================
# A. read_memory meta shape
# =====================================================================
section("A. read_memory meta carries posture metadata")

spec = QuerySpec(
    cycle_id="test-posture-meta-001",
    text="who is the operator",
    limit=5,
)
res = read_memory(spec)
meta = res.meta

check("A1 meta has posture", "posture" in meta)
check("A2 meta has recall_plan", "recall_plan" in meta)
check("A3 meta has recall_receipt", "recall_receipt" in meta)

if "posture" in meta:
    check("A4 posture is IDENTITY_RESOLUTION", meta["posture"] == "IDENTITY_RESOLUTION")

if "recall_receipt" in meta:
    receipt = meta["recall_receipt"]
    check("A5 receipt has query", isinstance(receipt.get("query"), str))
    check("A6 receipt has posture", isinstance(receipt.get("posture"), str))
    check("A7 receipt has winning_memory_ids", isinstance(receipt.get("winning_memory_ids"), list))
    check("A8 receipt has why_selected", isinstance(receipt.get("why_selected"), dict))


# =====================================================================
# B. Backward compatibility
# =====================================================================
section("B. Backward compatibility")

check("B1 meta still has seed_label", "seed_label" in meta)
check("B2 meta still has seed_coord", "seed_coord" in meta)
check("B3 meta still has seed_confidence", "seed_confidence" in meta)
check("B4 candidates is list", isinstance(res.candidates, list))
check("B5 result cycle_id matches", res.cycle_id == spec.cycle_id)


# =====================================================================
# C. Suppression behavior (expected after Claude fill)
# =====================================================================
section("C. Suppression behavior")

# The walker still returns candidates; posture-aware scoring should
# suppress tool/workflow/code lobes for IDENTITY_RESOLUTION.
# Until Claude fills lane execution, we verify the metadata only.
if "recall_plan" in meta:
    plan = meta["recall_plan"]
    check("C1 plan lane_weights suppress tools", plan.get("lane_weights", {}).get("tools") == 0.0)
    check(
        "C2 plan lane_weights suppress workflows",
        plan.get("lane_weights", {}).get("workflows") == 0.0,
    )


# =====================================================================
# D. Multi-posture fallback metadata
# =====================================================================
section("D. Multi-posture fallback metadata")

spec_vague = QuerySpec(
    cycle_id="test-posture-meta-002",
    text="operator",
    limit=5,
)
res_vague = read_memory(spec_vague)
meta_vague = res_vague.meta

check("D1 vague query has posture meta", "posture" in meta_vague)
if "posture" in meta_vague:
    check(
        "D2 vague query posture is MULTI_POSTURE or fallback",
        meta_vague["posture"] in ("MULTI_POSTURE", "BACKGROUND_LOOKUP"),
    )


# =====================================================================
# E. Receipt determinism
# =====================================================================
section("E. Receipt determinism")

res2 = read_memory(spec)
meta2 = res2.meta
if "recall_receipt" in meta and "recall_receipt" in meta2:
    r1 = meta["recall_receipt"]
    r2 = meta2["recall_receipt"]
    check("E1 same query -> same receipt posture", r1["posture"] == r2["posture"])


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
