"""
test_recall_posture_code_gate.py — regression gate for the deterministic
code/workflow recall-posture gate (memory/recall_posture.py).

Operator (2026-05-18): "just add deterministic script gating for code
and its subdirectories."

Defect this guards: the 12-lobe restructure added coding(8)+workflows
(10) to ingest but select_recall_posture (keyword-only, no classifier)
had no rule for them, so code/workflow queries fell to the identity-
family BACKGROUND_LOOKUP fallback whose suppressed_lobes include 8/10
=> coding/workflows memories were write-perfect but recall-suppressed.

Fix: a deterministic rule whose vocabulary is DERIVED from dewey_map
SUBDOMAIN_MAP[8]/[10] (single source of truth, cannot drift) routes
code/workflow queries to PROCEDURAL_HOW (preferred lobes [5,8,10]).
This gate must (a) fire for code/workflow queries, (b) NOT steal the
already-perfect identity/knowledge/situational paths (zero regression).

No runtime needed (pure deterministic rules). ASCII only.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging

log = logging.getLogger("omega.tests.memory.test_recall_posture_code_gate")

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


from memory.recall_posture import (  # noqa: E402
    select_recall_posture,
    _CODE_WORKFLOW_PATTERNS,
    POSTURE_CATALOG,
)


def _primary(q: str) -> str:
    """The posture compile_recall_plan will actually use: the chosen
    posture, or (when MULTI_POSTURE) secondary_postures[0]."""
    d = select_recall_posture(q)
    if d.posture == "MULTI_POSTURE":
        return d.secondary_postures[0] if d.secondary_postures else "BACKGROUND_LOOKUP"
    return d.posture


# ── vocabulary is dewey-derived, not hand-listed ───────────────────
print("\n[source-of-truth] gate vocabulary is derived from dewey_map")
import memory.dewey_map as dm  # noqa: E402

lang8 = {str(v).split("_", 1)[0] for (lobe, _y), v in dm.SUBDOMAIN_MAP.items() if lobe == 8}
wf10 = {str(v).replace("_", " ") for (lobe, _y), v in dm.SUBDOMAIN_MAP.items() if lobe == 10}
check(
    "a python language form is in the derived patterns",
    any("python" in p for p in _CODE_WORKFLOW_PATTERNS),
)
check(
    "every dewey lobe-10 workflow-kind phrase is in the patterns",
    wf10.issubset(set(_CODE_WORKFLOW_PATTERNS)),
    detail=f"missing={wf10 - set(_CODE_WORKFLOW_PATTERNS)}",
)
check(
    "PROCEDURAL_HOW posture prefers lobes 8 AND 10 (surfaces them)",
    8 in POSTURE_CATALOG["PROCEDURAL_HOW"].preferred_lobes
    and 10 in POSTURE_CATALOG["PROCEDURAL_HOW"].preferred_lobes,
)

# ── the gate FIRES for code / workflow queries ─────────────────────
print("\n[gate fires] code/workflow queries route to PROCEDURAL_HOW")
CODE_Q = [
    "asyncio gather coroutine aiohttp ClientSession pool",
    "python ExitStack nested context manager resource cleanup",
    "how do i refactor this rust trait implementation",
    "debug a segfault in my c++ template code",
    "kubernetes docker ci/cd pipeline for the service",
    "first write a failing unit test then implement then refactor",
    "typescript react component with hooks",
]
for q in CODE_Q:
    p = _primary(q)
    check(f"code/wf query -> PROCEDURAL_HOW | {q[:46]}", p == "PROCEDURAL_HOW", detail=f"got {p}")

# ── zero regression: the already-perfect paths are untouched ───────
print("\n[no regression] identity/knowledge/situational unaffected")
NON_CODE = [
    ("who are you and what is your purpose", "IDENTITY_RESOLUTION"),
    ("explain ATP synthase and oxidative phosphorylation", "KNOWLEDGE_EXPLANATION"),
    ("what is quantum entanglement", "KNOWLEDGE_EXPLANATION"),
    ("what happened when the migration failed", "TEMPORAL_RECALL"),
]
for q, expect in NON_CODE:
    p = _primary(q)
    check(
        f"non-code query NOT hijacked by code gate (-> {p}, want {expect}) | {q[:40]}",
        p == expect,
        detail=f"got {p}",
    )


# A non-code query must match ZERO code patterns (the structural
# guarantee that high gate weight cannot over-trigger).
def _code_hits(q: str) -> int:
    ql = q.lower()
    return sum(1 for pat in _CODE_WORKFLOW_PATTERNS if pat in ql)


for q, _ in NON_CODE:
    check(
        f"non-code query matches 0 code patterns | {q[:40]}",
        _code_hits(q) == 0,
        detail=f"hits={_code_hits(q)}",
    )

print(f"\nTOTAL  PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
