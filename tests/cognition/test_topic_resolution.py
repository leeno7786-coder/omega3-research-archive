"""Topic Resolution Engine — Phase 1 acceptance + cascade skip tests."""

from __future__ import annotations

import cognition_chain as cl
from meta_loop._meta_a_state import MetaAState
from meta_loop.intake.run import MetaAInput
from meta_loop.meta_a import build_execution_action_pack, execution_action_pack_to_dict
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import EpisodeStore
from tests.helpers.pipeline_ownership import bind_ignition


def make_meta_a_state(
    *,
    target_str: str = "",
    text: str = "",
    meta: dict | None = None,
    mi: MetaAInput | None = None,
) -> MetaAState:
    if mi is None:
        mi = MetaAInput(
            episode_id="ep-test",
            pressure={"reason": "test", "contributors": []},
            trajectory_snapshot={},
            action_goal={
                "episode_id": "ep-test",
                "intent": "LEARN",
                "target": target_str,
                "confidence": 0.9,
                "reason": "test",
            },
        )
    return MetaAState(
        mi=mi,
        episode_id="ep-test",
        intent="LEARN",
        action="USE_TOOL",
        source={},
        text=text,
        target_str=target_str,
        constraints={},
        meta=meta or {},
    )


def test_operator_query_resolves_transformers():
    from cognition.topic_resolution.resolve_topic import resolve_learning_topic

    mi = MetaAInput(
        episode_id="ep-test",
        pressure={
            "reason": "operator_query",
            "contributors": [
                {
                    "source": "operator",
                    "id": "operator_query",
                    "weight": 1.0,
                    "metadata": {"message": "How do transformers work?"},
                }
            ],
        },
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep-test",
            "intent": "LEARN",
            "target": "How do transformers work?",
            "confidence": 0.9,
            "reason": "operator_query",
        },
    )
    state = make_meta_a_state(target_str="How do transformers work?", mi=mi)
    resolved = resolve_learning_topic(state)
    assert resolved.topic == "transformers"
    assert resolved.source == "operator_query"
    assert resolved.domain == "transformers"


def test_open_question_highest_score(monkeypatch):
    from datetime import datetime, timezone

    from cognition.topic_resolution.resolve_topic import resolve_learning_topic
    from pressure_loop.open_questions import OpenQuestion

    asked = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    def _fake_list_open():
        return [
            OpenQuestion(
                question_id="q1",
                curiosity_id="c1",
                act="follow_up_question",
                question_text="retrieval drift",
                asked_cycle="cycle-1",
                asked_at=asked,
                status="open",
            )
        ]

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        _fake_list_open,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.now_iso",
        lambda: datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {},
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        lambda path=None: {},
    )
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

    state = make_meta_a_state(target_str="", text="")
    resolved = resolve_learning_topic(state)
    assert resolved.topic == "retrieval drift"
    assert resolved.source == "open_question"


def test_mastery_gap_picks_largest_gap(monkeypatch):
    from cognition.topic_resolution.resolve_topic import resolve_learning_topic
    from deepsleep_loop._deepsleep_state import MasteryRecord

    def _fake_load_mastery(path=None):
        return {
            "attention": MasteryRecord(
                domain_id="attention",
                domain_name="attention",
                knowledge_mastery=0.14,
                status="learning",
            ),
            "transformers": MasteryRecord(
                domain_id="transformers",
                domain_name="transformers",
                knowledge_mastery=0.33,
                status="learning",
            ),
        }

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        _fake_load_mastery,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: [],
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {},
    )
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

    state = make_meta_a_state(target_str="", text="")
    resolved = resolve_learning_topic(state)
    assert resolved.topic == "attention"
    assert resolved.source == "mastery_gap"


def test_all_sources_empty_returns_no_topic_available(monkeypatch):
    from cognition.topic_resolution.resolve_topic import (
        NO_TOPIC_AVAILABLE,
        resolve_learning_topic,
    )

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: [],
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        lambda path=None: {},
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {},
    )
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

    state = make_meta_a_state(target_str="", text="", meta={})
    resolved = resolve_learning_topic(state)
    assert resolved.topic == NO_TOPIC_AVAILABLE
    assert resolved.source == "none"


def test_learn_no_topic_skips_to_idle_pack(monkeypatch):
    from cognition.topic_resolution.resolve_topic import NO_TOPIC_AVAILABLE, ResolvedTopic

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.resolve_learning_topic",
        lambda state: ResolvedTopic(
            topic=NO_TOPIC_AVAILABLE,
            domain="",
            source="none",
            confidence=0.0,
        ),
    )

    mi = MetaAInput(
        episode_id="ep-skip",
        pressure={"reason": "learning_debt", "contributors": []},
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep-skip",
            "intent": "LEARN",
            "target": "",
            "confidence": 0.5,
            "reason": "autonomous learn with no target",
        },
    )
    pack = build_execution_action_pack(mi)
    d = execution_action_pack_to_dict(pack)
    assert d["action"] == "IDLE"
    assert d["tool"] is None
    assert d["execution_intent"] == "SKIP_NO_TOPIC"
    assert "learning material" not in str(d.get("execution_inputs") or {})


def test_learn_cascade_closes_without_tool_execution(monkeypatch):
    """Full LEARN tick with all six sources empty closes cleanly."""
    store = EpisodeStore()
    auto_ran: list[str] = []
    omega_ran: list[str] = []
    captured_packs: list[dict] = []

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: [],
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        lambda path=None: {},
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {},
    )
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


    def _global_learn(ep, ign):
        ep.global_ = dict(ep.global_ or {})
        ep.global_["action_goal"] = {
            "episode_id": ep.episode_id,
            "intent": "LEARN",
            "target": "",
            "confidence": 0.5,
            "reason": "no resolvable topic",
        }
        return ep.global_["action_goal"]

    def _auto(ep):
        auto_ran.append("AUTO")
        ep.auto = {"cycle_kind": "skipped", "status": "skipped"}
        return ep.auto

    from meta_loop.loop import run_meta_a as real_run_meta_a

    def _meta_capture(ep):
        result = real_run_meta_a(ep)
        captured_packs.append(dict((ep.meta or {}).get("execution_action_pack") or {}))
        return result

    monkeypatch.setattr("global_loop.loop.run_global_a", _global_learn)
    monkeypatch.setattr("meta_loop.loop.run_meta_a", _meta_capture)
    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto)
    # The verdict the real Meta B writes for a skipped cycle. WO-06's outcome extractor reads
    # it at close, and an execution block with no result is refused rather than guessed at.
    monkeypatch.setattr(
        "meta_loop.meta_b.run_meta_b_front",
        lambda ep: (
            setattr(ep, "meta", {"reflection": {"execution": {
                "executed": False, "result": "none", "status": "skipped",
                "disposition": "skipped"}}})
            or ep.meta["reflection"]
        ),
    )
    def _global_b(ep, _reflection):
        ep.global_ = dict(ep.global_ or {})
        ep.global_["omega_packet"] = {
            "episode_id": ep.episode_id,
            "intent": "LEARN",
            "action": "NO_OP",
            "domain": "",
        }
        return ep.global_["omega_packet"]

    monkeypatch.setattr("global_loop.loop.run_global_b", _global_b)
    monkeypatch.setattr(
        cl,
        "_run_omega_episode_cognition",
        lambda ep: omega_ran.append(ep.episode_id),
    )

    ignition = EpisodeIgnition(
        episode_id="ep-no-topic",
        aggregate_pressure=0.8,
        opened_by="learning_debt",
        dominant_contributor=make_contributor("sys", "learning_debt", 0.8, {"lane": "chat"}),
        contributors=(make_contributor("sys", "learning_debt", 0.8, {"lane": "chat"}),),
        trajectory_snapshot=None,
    )

    cl.ignite_episode(store, bind_ignition(ignition))

    assert store.current() is None
    assert auto_ran == ["AUTO"]
    assert len(omega_ran) == 1
    assert captured_packs
    pack = captured_packs[-1]
    assert pack["action"] == "IDLE"
    assert not pack.get("tool")
    assert pack.get("execution_intent") == "SKIP_NO_TOPIC"
