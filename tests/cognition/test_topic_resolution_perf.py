"""Topic resolution performance — single journal read per resolve pass."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_pick_critical_open_question_reads_journal_once():
    from cognition.topic_resolution import resolve_topic as rt

    trajectory = {"target_domain": "transformers"}
    calls = {"n": 0}
    real = rt._blockage_recency_weights

    def _counting(*, window, decay):
        calls["n"] += 1
        return real(window=window, decay=decay)

    with patch.object(rt, "list_open", return_value=[]):
        with patch.object(rt, "_blockage_recency_weights", side_effect=_counting):
            rt._pick_critical_open_question(trajectory)
    assert calls["n"] == 0  # no questions → no journal read

    q = MagicMock(
        status=rt.STATUS_OPEN,
        windows_survived=5,
        question_text="how do attention heads work?",
        question_id="q1",
    )
    with patch.object(rt, "list_open", return_value=[q]):
        with patch.object(rt, "_question_trajectory_relevance", return_value=1.0):
            with patch.object(rt, "_blockage_recency_weights", side_effect=_counting):
                with patch.object(rt, "load_failures", return_value={}):
                    rt._pick_critical_open_question(trajectory)
    assert calls["n"] == 1
