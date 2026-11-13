"""Topic resolution mastery and episode-context contracts."""

from __future__ import annotations

import pytest

from deepsleep_loop._deepsleep_state import MasteryRecord
from deepsleep_loop.mastery.run import _episode_target, update_mastery
from pressure_loop.episode import CognitiveEpisode
from pressure_loop.episode_journal import enriched_episode_from_episode
from tests.core.deepsleep_window_fixtures import producer_outcome_axes, window_input


def _learn_entry_with_resolved_domain(
    *,
    resolved_domain: str = "transformers",
    raw_target: str = "learning material",
) -> dict:
    return {
        "episode_id": "ep-learn-resolved",
        "intent": "LEARN",
        "outcome": {
            "goal": {"intent": "LEARN", "target": raw_target},
            "outcome": {"goal_state": "completed", "status": "success"},
        },
        "outcome_axes": producer_outcome_axes(success=True),
        "execution": {
            "execution_action_pack": {
                "execution_inputs": {
                    "query": "How do transformers work?",
                    "domain": resolved_domain,
                    "topic_source": "operator_query",
                }
            }
        },
    }


def test_episode_target_prefers_human_query_over_resolved_domain_slug():
    entry = _learn_entry_with_resolved_domain()
    assert _episode_target(entry, fallback="learning material") == "How do transformers work?"


def test_update_mastery_grounds_on_resolved_domain_absolute(tmp_path, monkeypatch):
    mastery_path = tmp_path / "mastery.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: mastery_path,
    )

    entry = _learn_entry_with_resolved_domain()
    entry["omega_result"] = {
        "mastery_effects": [
            {
                "domain": "transformers",
                "knowledge_mastery": 0.45,
                "application_mastery": 0.0,
                "rationale": "covered attention basics, not training dynamics",
            }
        ]
    }
    ds_input = window_input(
        window_id="win-mastery",
        episodes=[entry],
        reflection={"trajectory": {"target_domain": "learning material"}},
    )

    records = update_mastery(ds_input, mastery_path=mastery_path)
    assert len(records) == 1
    record = records[0]
    assert record.domain_name == "transformers"
    assert record.domain_id == "transformers"
    assert record.knowledge_mastery == pytest.approx(0.45)


def test_resolve_mastery_gap_returns_human_domain_name_not_slug(monkeypatch):
    from cognition.topic_resolution.resolve_topic import resolve_learning_topic
    from tests.cognition.test_topic_resolution import make_meta_a_state

    human_topic = "Knowledge Acquisition/Domain Mastery"
    slug = "knowledge-acquisition-domain-mastery"

    def _fake_load_mastery(path=None):
        return {
            slug: MasteryRecord(
                domain_id=slug,
                domain_name=human_topic,
                knowledge_mastery=0.10,
                target_mastery=0.70,
                status="learning",
            )
        }

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        _fake_load_mastery,
    )
    monkeypatch.setattr("cognition.topic_resolution.resolve_topic.list_open", lambda: [])
    monkeypatch.setattr("cognition.topic_resolution.resolve_topic.read_trajectory", lambda: {})
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.build_episode_learning_state",
        lambda **kwargs: {
            "learning_debt_flags": {
                "empty_substrate": False,
                "stale_mastery": False,
                "low_confidence_recall": False,
            },
            "mastery": {"top_weak": []},
        },
    )

    resolved = resolve_learning_topic(make_meta_a_state(target_str="", text=""))
    assert resolved.topic == human_topic
    assert resolved.source == "mastery_gap"


def test_enriched_episode_topic_history_from_execution_inputs():
    ep = CognitiveEpisode(
        episode_id="ep-topic-history",
        opened_by="learning_debt",
        pressure={},
        global_={
            "action_goal": {"intent": "LEARN", "target": "learning material"},
            "outcome": {"status": "success"},
        },
        meta={
            "execution_action_pack": {
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "execution_inputs": {
                    "query": "How do transformers work?",
                    "domain": "transformers",
                    "topic_source": "trajectory",
                },
            }
        },
    )

    body = enriched_episode_from_episode(ep)
    assert body["topic_history"] == {
        "topic": "How do transformers work?",
        "domain": "transformers",
        "source": "trajectory",
    }


def test_enriched_episode_topic_history_none_without_resolved_domain():
    ep = CognitiveEpisode(
        episode_id="ep-no-topic",
        opened_by="learning_debt",
        pressure={},
        global_={
            "action_goal": {"intent": "LEARN", "target": ""},
            "outcome": {"status": "skipped"},
        },
        meta={"execution_action_pack": {"action": "IDLE", "tool": None}},
    )

    body = enriched_episode_from_episode(ep)
    assert body.get("topic_history") is None
