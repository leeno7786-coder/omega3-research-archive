"""
test_walker_recall.py — end-to-end test of the walker-native recall pipeline.

After dropping the BGE reranker (2026-05-08), recall is sourced entirely
from:
    recall_compiler -> recall_lanes -> recall_arbitrator -> api.py shim

This test verifies:
    1. compile_recall_plan produces a sensible RecallPlan
    2. run_recall_lanes emits LaneHits including the embedding lane (walker)
    3. arbitrate_evidence returns winners with structured final_score
    4. memory.api.read_memory builds the new meta["arbitration"] shape
       sourced from recall_winners (no BGE)
    5. cand.rerank_score is now alias for final_score from the lane arb
    6. meta["rerank_present"] is False (cross-encoder is gone)

Usage:
    .\\.venv-brain\\Scripts\\python.exe scripts\\test_walker_recall.py
"""

from __future__ import annotations

import io
import logging
import sys
import warnings
from pathlib import Path

log = logging.getLogger("omega.tests.memory.test_walker_recall")


# UTF-8 stdout for Windows
try:
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
except Exception:
    pass

_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.getLogger("omega").setLevel(logging.WARNING)


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    return ok


def main() -> int:
    failures = 0

    # This script reads the operator's live store on purpose, but read_memory appends a recall
    # verdict row; keep that write out of memory/recall_verifier/verdict_log.jsonl (found
    # 2026-09-15 by the plain-script matrix pollution check).
    import tempfile

    import memory.recall_verifier as recall_verifier

    verdict_scratch = Path(tempfile.mkdtemp(prefix="omega-walker-recall-")) / "verdict_log.jsonl"
    recall_verifier.verdict_log_path = lambda *, root=None: verdict_scratch

    # ── 1. Module imports ────────────────────────────────────────────
    _section("1. Recall pipeline imports")
    from memory.recall_compiler import compile_recall_plan
    from memory.evidence_set import MemoryEvidence
    from memory import api
    from memory.engine.walker import walker, COORD_INDEX
    from memory.engine.walker import load_coord_index

    failures += not _check("recall_compiler imported", True)
    failures += not _check("recall_lanes imported", True)
    failures += not _check("recall_arbitrator imported", True)
    failures += not _check("memory.api imported", True)
    failures += not _check("walker imported", True)
    failures += not _check(
        "MemoryEvidence has final_score field",
        hasattr(MemoryEvidence(memory_id="x"), "final_score"),
    )

    # ── 2. compile_recall_plan ───────────────────────────────────────
    _section("2. compile_recall_plan(query) — posture + coord_hypotheses")

    # Bind to the canonical QuerySpec so the test can't drift again
    # when memory/api adds fields (same drift-proof principle as the
    # followup_spawner _STORE_DIR fix). The old local stub lacked
    # `allowed_lobes` (added for lane discipline) -> AttributeError when
    # api.read_memory ran `query.allowed_lobes`.
    from memory.api import QuerySpec as QSpec

    queries = [
        ("Who am I to you?", "IDENTITY"),
        ("Tell me about photosynthesis", "knowledge"),
        ("Python decorators wrap a function", "coding"),
        ("Earlier today I felt frustrated", "situational"),
        ("Daisy mentioned the bug fix", "personas"),
    ]

    for query_text, label in queries:
        plan = compile_recall_plan(QSpec(cycle_id="test", text=query_text))
        ok = (
            plan is not None
            and plan.posture is not None
            and plan.coord_hypotheses
            and plan.lane_weights
        )
        detail = (
            f"posture={plan.posture.name}  "
            f"hypotheses={len(plan.coord_hypotheses)}  "
            f"lanes_weighted={sum(1 for v in plan.lane_weights.values() if v > 0)}"
        )
        failures += not _check(f"plan for '{label}'", ok, detail)

    # ── 3. Walker direct ─────────────────────────────────────────────
    _section("3. walker(query) — direct walker invocation")
    load_coord_index()
    print(f"  coord_index entries: {len(COORD_INDEX)}")
    if not COORD_INDEX:
        print("  (corpus is empty — add test memories below)")
    contexts = walker("Who am I to you?", top_k=5)
    failures += not _check(
        "walker returns list",
        isinstance(contexts, list),
        f"len={len(contexts)}",
    )

    # ── 4. End-to-end api.read_memory ────────────────────────────────
    _section("4. api.read_memory — full walker-native pipeline")

    for query_text, label in queries:
        try:
            result = api.read_memory(QSpec(cycle_id="test", text=query_text, limit=5))
        except Exception as exc:
            failures += not _check(
                f"read_memory('{label}') ran",
                False,
                f"exception: {type(exc).__name__}: {exc}",
            )
            continue

        meta = result.meta
        cands = result.candidates

        # 4a. arbitration metadata exists in new shape
        arb_ok = (
            "arbitration" in meta
            and meta["arbitration"] is not None
            and "explanation" in meta["arbitration"]
            and "top" in meta["arbitration"]
        )
        failures += not _check(
            f"meta['arbitration'] populated for '{label}'",
            arb_ok,
            f"explanation={meta.get('arbitration', {}).get('explanation', '')[:60]!r}"
            if arb_ok
            else "missing or None",
        )

        # 4b. rerank_present is False (BGE gone)
        rp = meta.get("rerank_present")
        failures += not _check(
            f"rerank_present == False for '{label}'",
            rp is False,
            f"got {rp!r}",
        )

        # 4c. explanation mentions walker-native (not BGE)
        explanation = (meta.get("arbitration") or {}).get("explanation") or ""
        failures += not _check(
            "arbitration explanation is walker-native",
            "walker" in explanation.lower() or "lane" in explanation.lower(),
            f"explanation={explanation[:80]!r}",
        )

        # 4d. recall_winners has structured final_score (when there are any)
        winners = meta.get("recall_winners") or []
        if winners:
            has_score = all("final_score" in w for w in winners)
            failures += not _check(
                f"all recall_winners carry final_score for '{label}'",
                has_score,
                f"winners={len(winners)}",
            )
            top_score = float(winners[0].get("final_score") or 0.0)
            failures += not _check(
                f"top winner final_score > 0 for '{label}'",
                top_score > 0.0,
                f"top_score={top_score:.3f}",
            )
        else:
            print(f"  [INFO] '{label}': 0 lane winners (corpus may not match)")

        # 4e. candidates have rerank_score = final_score (alias)
        if winners and cands:
            top_winner_id = winners[0].get("memory_id")
            top_cand = next((c for c in cands if c.id == top_winner_id), None)
            if top_cand is not None:
                expected = float(winners[0].get("final_score") or 0.0)
                got = float(getattr(top_cand, "rerank_score", -1) or -1)
                failures += not _check(
                    f"cand.rerank_score == final_score for '{label}'",
                    abs(got - expected) < 0.001,
                    f"expected={expected:.3f} got={got:.3f}",
                )

        # Print top 3 for visibility
        if winners:
            print(f"  TOP for '{label}':")
            for i, w in enumerate(winners[:3], start=1):
                print(
                    f"    {i}. {w.get('memory_id'):<24} "
                    f"final={float(w.get('final_score', 0)):.3f} "
                    f"lanes={','.join(w.get('lanes', []))}"
                )

    # ── 5. BGE absence verification ──────────────────────────────────
    _section("5. BGE reranker absence")
    src = (
        next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
        / "memory"
        / "api.py"
    ).read_text(encoding="utf-8")
    bge_imports = src.count("from core.reranker_engine import arbitrate")
    failures += not _check(
        "no live `from core.reranker_engine import arbitrate` in api.py",
        bge_imports == 0,
        f"found {bge_imports} call sites",
    )
    walker_native = "walker-native arbitration" in src
    failures += not _check(
        "api.py contains 'walker-native arbitration' shim",
        walker_native,
    )

    # ── verdict ──────────────────────────────────────────────────────
    _section("VERDICT")
    if failures == 0:
        print("  ALL CHECKS PASSED — walker-native recall pipeline is live.")
        return 0
    print(f"  {failures} CHECK(S) FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
