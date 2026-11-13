"""Audit substrate MEDIUM/LOW regression tests (2026-06-23)."""

from __future__ import annotations

from pressure_loop.contributors import make_contributor
from pressure_loop.cognition_queue import _box_dedup_key, enqueue_input


def test_operator_response_dedup_uses_system_query_id_not_message_text():
    """MEDIUM-1: identical answer text to different questions must not collide."""
    q1 = make_contributor(
        "operator",
        "operator_response",
        1.0,
        {
            "message": "yes",
            "cycle_id": "c-1",
            "system_query_id": "sq-1",
        },
    )
    q2 = make_contributor(
        "operator",
        "operator_response",
        1.0,
        {
            "message": "yes",
            "cycle_id": "c-2",
            "system_query_id": "sq-2",
        },
    )
    assert _box_dedup_key(q1) != _box_dedup_key(q2)

    enqueue_input(q1, operator_activity=False)
    assert enqueue_input(q2, operator_activity=False) is True


def test_dopamine_plateau_requires_measured_history(monkeypatch):
    """MEDIUM-4: absent dopamine history must not fabricate plateau."""
    from autonomic.cognitive_churn import assess_cognitive_churn, reset_churn_cache_for_tests

    reset_churn_cache_for_tests()
    monkeypatch.setattr(
        "autonomic.cognitive_churn._load_mastery_scores",
        lambda mastery_path=None: {},
    )
    monkeypatch.setattr(
        "autonomic.cognitive_churn._read_recent_outcomes",
        lambda outcomes_path, limit=40: [{"domain": "novel_domain"}],
    )
    monkeypatch.setattr(
        "autonomic.cognitive_churn._summarise_outcomes",
        lambda outcomes: {
            "by_domain": {"novel_domain": {"attempts": 5, "satisfied": 3}},
            "by_goal_id": {},
        },
    )
    monkeypatch.setattr(
        "autonomic.cognitive_churn._dopamine_avg_by_domain",
        lambda cycle_limit=30: {},
    )
    monkeypatch.setattr(
        "autonomic.cognitive_churn._content_fingerprints_by_domain",
        lambda outcomes, cycle_limit=30: {},
    )

    snap = assess_cognitive_churn(force_refresh=True)
    churn = snap.by_domain["novel_domain"]
    assert churn.dopamine_avg == 0.0
    assert churn.dopamine_plateau is False


def test_situational_success_does_not_substring_match_ok_in_broke():
    """MEDIUM-6: 'ok' cue must not match inside 'broke'."""
    from memory.enrichment.situational import _pick_success

    assert _pick_success("the build broke", None) is not True


def test_consecutive_failures_skips_neutral_cycle_kinds():
    """LOW-10: skipped/idle cycles are neutral — not counted as failures."""
    from autonomic._history import consecutive_failures

    cycles = [
        {"score": {"cycle_kind": "skipped"}},
        {"score": {"cycle_kind": "fail"}},
        {"score": {"cycle_kind": "success"}},
    ]
    assert consecutive_failures(cycles) == 1


def test_contradiction_pass_retains_post_commit_candidacy(tmp_path, monkeypatch):
    """MEDIUM-9: contradiction pass must not drain post_commit candidacy rows."""
    import json

    import memory.queues as q

    monkeypatch.setattr(q, "DEEPSLEEP_QUEUE_PATH", tmp_path / "deepsleep_candidates.jsonl")
    monkeypatch.setattr(q, "_QUEUES_DIR", tmp_path)
    q.DEEPSLEEP_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)

    rows = [
        {
            "memory_id": "m1",
            "reason": "contradiction:canonical=c1;evidence=e1",
        },
        {
            "memory_id": "m2",
            "reason": "post_commit_candidacy",
        },
    ]
    q.DEEPSLEEP_QUEUE_PATH.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )

    from memory.contradiction_resolution import process_contradiction_candidates

    monkeypatch.setattr(
        "memory.contradiction_resolution._load_memory_document",
        lambda _mid: None,
    )
    process_contradiction_candidates(max_groups=0)

    remaining = [
        json.loads(line)
        for line in q.DEEPSLEEP_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(remaining) == 1
    assert remaining[0]["memory_id"] == "m2"
