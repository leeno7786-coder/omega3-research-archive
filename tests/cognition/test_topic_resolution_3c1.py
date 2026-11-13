"""Topic Resolution Phase 3c.1 — severe mastery gap + trajectory hard default."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cognition.topic_resolution.resolve_topic import (
    _resolve_mastery_gap,
    _resolve_trajectory,
    resolve_learning_topic,
)
from deepsleep_loop._deepsleep_state import MasteryRecord
from pressure_loop.open_questions import OpenQuestion
from tests.cognition.test_topic_resolution import make_meta_a_state


def test_mastery_gap_uses_max_knowledge_and_application_gap(monkeypatch):
    """knowledge=0.80 application=0.15 target=0.70 -> gap 0.55 (application bottleneck)."""

    def _fake_load_mastery(path=None):
        return {
            "bottleneck": MasteryRecord(
                domain_id="bottleneck",
                domain_name="application bottleneck",
                knowledge_mastery=0.80,
                application_mastery=0.15,
                target_mastery=0.70,
                status="learning",
            )
        }

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        _fake_load_mastery,
    )

    resolved = _resolve_mastery_gap()
    assert resolved is not None
    assert resolved.topic == "application bottleneck"
    assert resolved.confidence == pytest.approx(0.55)


def test_severe_mastery_gap_returns_topic_at_threshold(monkeypatch):
    def _fake_load_mastery(path=None):
        return {
            "severe": MasteryRecord(
                domain_id="severe",
                domain_name="severe domain",
                knowledge_mastery=0.15,
                application_mastery=0.15,
                target_mastery=0.70,
                status="learning",
            )
        }

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        _fake_load_mastery,
    )

    resolved = _resolve_mastery_gap()
    assert resolved is not None
    assert resolved.topic == "severe domain"


def test_non_severe_mastery_gap_returns_none(monkeypatch):
    def _fake_load_mastery(path=None):
        return {
            "mild": MasteryRecord(
                domain_id="mild",
                domain_name="mild domain",
                knowledge_mastery=0.40,
                application_mastery=0.40,
                target_mastery=0.70,
                status="learning",
            )
        }

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        _fake_load_mastery,
    )

    assert _resolve_mastery_gap() is None


def test_trajectory_wins_over_open_question_when_mastery_not_severe(monkeypatch):
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

    def _fake_load_mastery(path=None):
        return {
            "mild": MasteryRecord(
                domain_id="mild",
                domain_name="mild domain",
                knowledge_mastery=0.40,
                application_mastery=0.40,
                target_mastery=0.70,
                status="learning",
            )
        }

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        _fake_list_open,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery",
        _fake_load_mastery,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {"target_domain": "attention mechanisms"},
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
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.now_iso",
        lambda: datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
    )

    resolved = resolve_learning_topic(make_meta_a_state(target_str="", text=""))
    assert resolved.topic == "attention mechanisms"
    assert resolved.source == "trajectory"


def test_trajectory_resolution_confidence_is_hard_default(monkeypatch):
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {"target_domain": "transformers"},
    )

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert resolved.confidence == 0.85
