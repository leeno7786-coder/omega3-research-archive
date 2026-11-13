"""One recall lane must not starve another out of the shared evidence cap.

Live 2026-09-06: "Do you remember what I built yesterday?" ran the temporal
bundle. remember_time returned eight episode rows at confidence 1.0 and filled
the whole FINAL_RECALL_CANDIDATE_LIMIT; the single builder artifact that
remember_dev found — the actual answer — was dropped as over the limit, and the
model answered from the episode rows instead.
"""

from __future__ import annotations

from auto_loop.mril_tools.candidate_hygiene import FINAL_RECALL_CANDIDATE_LIMIT, finalize_recall_results

QUERY = "Do you remember what I built yesterday?"


def _row(memory_id: str, title: str, confidence: float, coord: list[int]) -> dict:
    return {
        "memory_id": memory_id,
        "coord": coord,
        "title": title,
        "remembered_as": title,
        "confidence": confidence,
        "relation_facets": [],
    }


def _results() -> list[dict]:
    time_rows = [
        _row(f"9.01.ep{i:02d}", f"RESPOND: operator turn {i} → omega: reply {i}", 1.0, [9, 1, 0])
        for i in range(FINAL_RECALL_CANDIDATE_LIMIT + 4)
    ]
    dev_rows = [_row("11.00.e718aec5", "Builder artifact: countdown.py", 0.5, [11, 0, 0])]
    return [
        {"tool": "remember_time", "verdict": "verified", "evidence": time_rows},
        {"tool": "remember_dev", "verdict": "verified", "evidence": dev_rows},
    ]


def test_every_lane_keeps_its_best_row_inside_the_shared_cap():
    finalized = finalize_recall_results(_results(), QUERY)
    by_tool = {r["tool"]: r for r in finalized}
    assert [e["memory_id"] for e in by_tool["remember_dev"]["evidence"]] == ["11.00.e718aec5"]
    assert by_tool["remember_dev"]["candidate_hygiene"] == {"retained": 1, "dropped": 0}
    total = sum(len(r["evidence"]) for r in finalized)
    assert total == FINAL_RECALL_CANDIDATE_LIMIT
    assert len(by_tool["remember_time"]["evidence"]) == FINAL_RECALL_CANDIDATE_LIMIT - 1


def test_a_single_lane_still_fills_the_cap_by_merit():
    results = [_results()[0]]
    finalized = finalize_recall_results(results, QUERY)
    ids = [e["memory_id"] for e in finalized[0]["evidence"]]
    assert len(ids) == FINAL_RECALL_CANDIDATE_LIMIT
    assert ids == [f"9.01.ep{i:02d}" for i in range(FINAL_RECALL_CANDIDATE_LIMIT)]
