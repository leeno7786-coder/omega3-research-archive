"""Richer autonomy output schema (2026-06-30): episode_assessment / what_changed /
trajectory_influence — validate-and-drop, parsed on LEARN + APPLY, taught to the agent lane by
_self_report_note."""

from __future__ import annotations

from omega_loop.episode import prompts as P
from omega_loop.episode.types import OmegaEpisodeInput


def test_filter_episode_assessment_valid_and_invalid():
    out = P._filter_episode_assessment({"completed": True, "quality": 0.8, "reason": "done"})
    assert out == {"completed": True, "quality": 0.8, "reason": "done"}
    out2 = P._filter_episode_assessment({"completed": 1, "quality": 5, "reason": ""})
    assert out2["completed"] is True and out2["quality"] == 1.0  # bool coerce + clamp
    assert P._filter_episode_assessment("nope") is None
    assert P._filter_episode_assessment(None) is None


def test_filter_what_changed_drops_when_empty():
    out = P._filter_what_changed({"learned": ["x"], "did": [], "verified": [], "failed": []})
    assert out == {"learned": ["x"], "did": [], "verified": [], "failed": []}
    assert P._filter_what_changed({"learned": [], "did": [], "verified": [], "failed": []}) is None
    assert P._filter_what_changed("bad") is None
    out2 = P._filter_what_changed({"did": [1, 2, "three"]})  # coerce to str
    assert out2["did"] == ["1", "2", "three"]


def test_filter_trajectory_influence_decision_clamped():
    out = P._filter_trajectory_influence({"decision": "PIVOT", "domain": "python", "reason": "r"})
    assert out == {"decision": "pivot", "domain": "python", "reason": "r"}
    assert P._filter_trajectory_influence({"decision": "explode"}) is None  # out of safe set
    assert (
        P._filter_trajectory_influence({"decision": "recall_first"})["decision"] == "recall_first"
    )
    assert P._filter_trajectory_influence(None) is None


def _payload():
    return {
        "knowledge_to_ingest": [],
        "mastery": {},
        "synthesis": "s",
        "pressure_params": {},
        "intent_decision": None,
        "episode_assessment": {"completed": True, "quality": 0.7, "reason": "ok"},
        "what_changed": {"learned": ["a"]},
        "trajectory_influence": {"decision": "continue", "domain": "python", "reason": "more"},
    }


def _packet():
    return OmegaEpisodeInput(
        episode_id="e1",
        intent="LEARN",
        action="learn",
        tool=None,
        domain="python",
        operator_text=None,
        outcome="ok",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=None,
    )


def test_parse_knowledge_captures_richer_fields():
    res = P._parse_knowledge(_payload(), _packet())
    assert res.episode_assessment["completed"] is True
    assert res.what_changed["learned"] == ["a"]
    assert res.trajectory_influence["decision"] == "continue"


def test_parse_apply_captures_richer_fields():
    res = P._parse_apply(_payload(), _packet())
    assert res.episode_assessment["quality"] == 0.7
    assert res.trajectory_influence["domain"] == "python"


def test_parse_omits_richer_fields_when_absent():
    res = P._parse_knowledge({"synthesis": "s"}, _packet())
    assert res.episode_assessment is None
    assert res.what_changed is None
    assert res.trajectory_influence is None
