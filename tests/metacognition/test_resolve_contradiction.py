"""
test_resolve_contradiction.py -- AUDIT-3 / Phase TEST

Tests the resolve_contradiction() implementation per audit issue H1
(production-required #1). Doctrine answered 2026-04-29:
  Guardian guards the boundary; the mind solves problems.
  Contradictions queue to DeepSleep candidates; DeepSleep restructures.
  resolve_contradiction does NOT call decide_request, EVER.

Six test groups:
  1. Public surface — ResolutionResult schema invariants
  2. Invalid canonical — returns clean result, queues nothing
  3. Invalid evidence (all) — returns clean result, queues nothing
  4. Mixed valid/invalid evidence — queues valid, surfaces invalid
  5. Happy path — canonical + N valid evidence, all queued, reason intact
  6. Doctrine guard — function does NOT touch guardian.decide_request
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ─────────────────────────────────────────────────────────────────
# Test scaffolding — write fake memory files into a tmp tree so
# path_for_id can resolve them. We patch the COORD_INDEX directly
# (cheaper than building real index files).
# ─────────────────────────────────────────────────────────────────
def make_fake_memory(tmpdir: Path, idstr: str, coord, text: str = "test") -> Path:
    """Write a minimally-valid memory file at tmpdir/<idstr>.json."""
    p = tmpdir / f"{idstr}.json"
    p.write_text(
        json.dumps(
            {
                "id": idstr,
                "coord": coord,
                "text": text,
                "payload": {"text": text},
            }
        ),
        encoding="utf-8",
    )
    return p


# =====================================================================
# 1. PUBLIC SURFACE
# =====================================================================
section("1. PUBLIC SURFACE")

import memory.api as api

check("resolve_contradiction is exported", hasattr(api, "resolve_contradiction"))
check("resolve_contradiction is callable", callable(getattr(api, "resolve_contradiction", None)))
check(
    "ResolutionResult has queued_to field", "queued_to" in api.ResolutionResult.__dataclass_fields__
)
check(
    "ResolutionResult has invalid_evidence_refs field",
    "invalid_evidence_refs" in api.ResolutionResult.__dataclass_fields__,
)


# =====================================================================
# 2. INVALID CANONICAL
# =====================================================================
section("2. INVALID CANONICAL")

# Patch path_for_id to return None for the canonical ref
with patch("memory.engine.walker.path_for_id", return_value=None) as _pf:
    r = api.resolve_contradiction("does_not_exist", ["e1", "e2"])

check("resolved=False on invalid canonical", r.resolved is False)
check("queued_to is empty on invalid canonical", r.queued_to == [])
check("notes mentions canonical resolution failure", "does not resolve" in r.notes)
check("evidence_refs preserved", r.evidence_refs == ["e1", "e2"])
check(
    "invalid_evidence_refs reflects all evidence (because canonical bailed)",
    r.invalid_evidence_refs == ["e1", "e2"],
)


# =====================================================================
# 3. INVALID EVIDENCE (all)
# =====================================================================
section("3. INVALID EVIDENCE (ALL)")

with tempfile.TemporaryDirectory() as tmp:
    tmpdir = Path(tmp)
    canonical_path = make_fake_memory(tmpdir, "0.00.0001", [0, 0, 1], "canonical text")

    # Make path_for_id return the canonical's path but None for evidence
    def _path_for(idstr):
        if idstr == "0.00.0001":
            return str(canonical_path)
        return None

    with patch("memory.engine.walker.path_for_id", side_effect=_path_for):
        r = api.resolve_contradiction(
            "0.00.0001",
            ["bogus_evidence_a", "bogus_evidence_b"],
        )

    check("resolved=False when no evidence resolves", r.resolved is False)
    check("queued_to is empty when no evidence resolves", r.queued_to == [])
    check("notes mentions 'no valid evidence'", "no valid evidence" in r.notes)
    check(
        "invalid_evidence_refs lists both invalid",
        set(r.invalid_evidence_refs) == {"bogus_evidence_a", "bogus_evidence_b"},
    )


# =====================================================================
# 4. MIXED VALID/INVALID EVIDENCE
# =====================================================================
section("4. MIXED VALID/INVALID EVIDENCE")

with tempfile.TemporaryDirectory() as tmp:
    tmpdir = Path(tmp)
    canonical_path = make_fake_memory(tmpdir, "0.00.0001", [0, 0, 1], "canonical")
    e_good_path = make_fake_memory(tmpdir, "0.00.0002", [0, 0, 2], "evidence good")

    def _path_for(idstr):
        return {
            "0.00.0001": str(canonical_path),
            "0.00.0002": str(e_good_path),
        }.get(idstr)

    # Force the deepsleep queue to a tmp file so this test doesn't
    # pollute real state.
    queue_path = tmpdir / "deepsleep_candidates.jsonl"
    with (
        patch("memory.engine.walker.path_for_id", side_effect=_path_for),
        patch("memory.queues.DEEPSLEEP_QUEUE_PATH", queue_path),
    ):
        r = api.resolve_contradiction(
            "0.00.0001",
            ["0.00.0002", "bogus_evidence"],
        )

    check("resolved=False on mixed input", r.resolved is False)
    check(
        "queued_to has canonical + valid evidence (2 entries)",
        set(r.queued_to) == {"0.00.0001", "0.00.0002"},
        f"got {r.queued_to}",
    )
    check(
        "invalid_evidence_refs lists only the bogus one",
        r.invalid_evidence_refs == ["bogus_evidence"],
    )
    check("notes surfaces invalid_evidence_skipped", "invalid_evidence_skipped" in r.notes)
    check("queue file exists on disk", queue_path.exists())

    # Verify queue contents
    queue_lines = queue_path.read_text(encoding="utf-8").strip().split("\n")
    check(
        "queue has exactly 2 entries (canonical + evidence)",
        len(queue_lines) == 2,
        f"got {len(queue_lines)}",
    )
    entries = [json.loads(line) for line in queue_lines]
    queued_ids = {e["memory_id"] for e in entries}
    check("queue entries cover canonical + evidence", queued_ids == {"0.00.0001", "0.00.0002"})
    check(
        "queue reason includes 'contradiction:'",
        all("contradiction:" in e["reason"] for e in entries),
    )
    check(
        "queue reason names canonical_ref",
        all("canonical=0.00.0001" in e["reason"] for e in entries),
    )


# =====================================================================
# 5. HAPPY PATH
# =====================================================================
section("5. HAPPY PATH (canonical + 3 evidence, all valid)")

with tempfile.TemporaryDirectory() as tmp:
    tmpdir = Path(tmp)
    canonical = make_fake_memory(tmpdir, "0.00.1001", [0, 0, 1001])
    e1 = make_fake_memory(tmpdir, "0.00.2001", [0, 0, 2001])
    e2 = make_fake_memory(tmpdir, "0.00.2002", [0, 0, 2002])
    e3 = make_fake_memory(tmpdir, "0.00.2003", [0, 0, 2003])

    def _path_for(idstr):
        return {
            "0.00.1001": str(canonical),
            "0.00.2001": str(e1),
            "0.00.2002": str(e2),
            "0.00.2003": str(e3),
        }.get(idstr)

    queue_path = tmpdir / "deepsleep_candidates.jsonl"
    with (
        patch("memory.engine.walker.path_for_id", side_effect=_path_for),
        patch("memory.queues.DEEPSLEEP_QUEUE_PATH", queue_path),
    ):
        r = api.resolve_contradiction(
            "0.00.1001",
            ["0.00.2001", "0.00.2002", "0.00.2003"],
        )

    check("resolved=False (function queues, doesn't resolve)", r.resolved is False)
    check(
        "queued_to has 4 ids (canonical + 3 evidence)", len(r.queued_to) == 4, f"got {r.queued_to}"
    )
    check("invalid_evidence_refs is empty", r.invalid_evidence_refs == [])
    check("notes mentions queueing count", "queued 4 ids to deepsleep_candidates" in r.notes)

    # Read back the journal
    queue_lines = queue_path.read_text(encoding="utf-8").strip().split("\n")
    check("journal has 4 entries", len(queue_lines) == 4)
    entries = [json.loads(line) for line in queue_lines]
    check(
        "each entry has memory_id, coord, queued_at, reason",
        all({"memory_id", "coord", "queued_at", "reason"} <= set(e.keys()) for e in entries),
    )
    check("each entry's coord is non-empty", all(len(e["coord"]) == 3 for e in entries))


# =====================================================================
# 6. DOCTRINE GUARD — must NOT touch guardian.decide_request
# =====================================================================
section("6. DOCTRINE GUARD")

# Inspect the source — resolve_contradiction must not import or call
# guardian.decide_request, decide_request, or guardian.decision.
import inspect

src = inspect.getsource(api.resolve_contradiction)

# Check for actual call sites + imports, NOT substring mentions —
# the docstring intentionally mentions "decide_request" to say
# explicitly that we DO NOT call it. Doctrine docs aren't violations.
import re
import logging

log = logging.getLogger("omega.tests.metacognition.test_resolve_contradiction")

calls_decide_request = bool(re.search(r"\bdecide_request\s*\(", src))
imports_guardian_decision = bool(
    re.search(
        r"^\s*(from\s+guardian\.decision|import\s+guardian\.decision|"
        r"from\s+guardian\s+import.*decide)",
        src,
        re.MULTILINE,
    )
)
check(
    "source has no decide_request(...) call site",
    not calls_decide_request,
    "found a decide_request(...) call invocation",
)
check(
    "source has no guardian.decision import",
    not imports_guardian_decision,
    "found guardian.decision import in resolve_contradiction",
)
check(
    "source explicitly invokes enqueue_deepsleep_candidate", "enqueue_deepsleep_candidate(" in src
)
check("docstring cites the doctrine memory", "feedback_guardian_boundary_not_cognition" in src)


# =====================================================================
# REPORT
# =====================================================================
print()
print("=" * 72)
print(f"TOTAL: PASS={PASS}  FAIL={FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
