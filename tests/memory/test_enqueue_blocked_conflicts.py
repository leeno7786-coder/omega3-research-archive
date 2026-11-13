"""Recall-time routing of quarantined conflicts to the DeepSleep queue.

The FROZEN verifier quarantines contradictions into RecallVerdict.blocked_conflicts; recall only
ROUTES them to DeepSleep's candidate queue (resolution is housekeeping, never at recall). The
planner groups the quarantined hits by title into (canonical_ref, evidence_refs) pairs with a
deterministic pivot; the enqueue wrapper hands each pair to memory.api.resolve_contradiction.
"""

from __future__ import annotations

from memory.contradiction_resolution import (
    enqueue_blocked_conflicts,
    plan_conflict_enqueues,
)


def _bc(mid, title):
    return {"memory_id": mid, "title": title}


def test_plan_groups_same_title_conflicts():
    plans = plan_conflict_enqueues([_bc("1.00.noah", "Operator"), _bc("1.00.kim", "operator ")])
    assert len(plans) == 1
    p = plans[0]
    assert p["canonical_ref"] == "1.00.kim"  # sorted-first pivot (deterministic anchor only)
    assert p["evidence_refs"] == ["1.00.noah"]


def test_plan_skips_singletons():
    assert plan_conflict_enqueues([_bc("1.00.only", "Operator")]) == []


def test_plan_separates_distinct_titles_and_drops_singleton_group():
    plans = plan_conflict_enqueues(
        [
            _bc("1.00.a", "Operator"),
            _bc("1.00.b", "Operator"),
            _bc("2.00.c", "Capital"),  # singleton on another title -> dropped
        ]
    )
    assert len(plans) == 1
    assert plans[0]["title"] == "operator"
    assert plans[0]["evidence_refs"] == ["1.00.b"]


def test_plan_dedupes_repeated_ids():
    plans = plan_conflict_enqueues([_bc("1.00.a", "T"), _bc("1.00.a", "T"), _bc("1.00.b", "T")])
    assert len(plans) == 1
    assert plans[0]["canonical_ref"] == "1.00.a"
    assert plans[0]["evidence_refs"] == ["1.00.b"]


def test_plan_is_deterministic():
    bc = [_bc("1.00.z", "T"), _bc("1.00.a", "T"), _bc("1.00.m", "T")]
    outs = [plan_conflict_enqueues(bc) for _ in range(3)]
    assert outs[0] == outs[1] == outs[2]
    assert outs[0][0]["canonical_ref"] == "1.00.a"


def test_enqueue_calls_resolve_contradiction(monkeypatch):
    calls = []

    class _Res:
        queued_to = ["x", "y"]

    monkeypatch.setattr(
        "memory.api.resolve_contradiction",
        lambda c, e: calls.append((c, tuple(e))) or _Res(),
    )
    out = enqueue_blocked_conflicts([_bc("1.00.noah", "Op"), _bc("1.00.kim", "Op")])
    assert out["groups"] == 1
    assert out["queued"] == 2
    assert calls == [("1.00.kim", ("1.00.noah",))]


def test_enqueue_is_best_effort_on_failure(monkeypatch):
    def _boom(c, e):
        raise RuntimeError("queue down")

    monkeypatch.setattr("memory.api.resolve_contradiction", _boom)
    out = enqueue_blocked_conflicts([_bc("1.00.a", "Op"), _bc("1.00.b", "Op")])
    assert out["groups"] == 0  # failure swallowed, recall never breaks
