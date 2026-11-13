"""RED-first acceptance for diff-review fixes (tonic_decay, 10th delta, fail-mode, fold domain, operator_query)."""

from __future__ import annotations

from datetime import datetime, timezone

import cognition_chain as cl
import pytest

from cognition.topic_resolution.resolve_topic import (
    _question_execution_blockage,
    _resolve_trajectory,
    resolve_learning_topic,
)
from deepsleep_loop._deepsleep_state import FailurePattern
from deepsleep_loop.failure.run import consolidate_failures
from knowledge_extractor.derive.run import _slug
from meta_loop.intake.run import MetaAInput
from pressure_loop.episode import EpisodeStore
from pressure_loop.open_questions import STATUS_OPEN, OpenQuestion
from pressure_loop.policy import pressure_neuromod_tonic_decay
from tests.cognition.test_topic_resolution import make_meta_a_state
from tests.core.deepsleep_window_fixtures import (
    episode_on_store,
    patch_window_deepsleep_paths,
)
from tests.core.test_deepsleep_failure import _failure_input


# --- HIGH-1: tonic_decay config value ---


def test_pressure_neuromod_tonic_decay_reads_symphony_yaml_decay_rate():
    cfg = pressure_neuromod_tonic_decay()
    assert cfg["decay_rate"] == pytest.approx(0.4)


# --- HIGH-2: per-episode DeepSleep store writes fail-hard ---


def test_per_episode_deepsleep_mastery_failure_propagates(tmp_path, monkeypatch):
    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)

    def _boom(store, *, cascade_status):
        raise RuntimeError("mastery write failed")

    monkeypatch.setattr(
        "deepsleep_loop.loop.run_deepsleep_for_episode",
        _boom,
    )

    store = EpisodeStore(
        reflection_window_path=paths["window_path"],
        trajectory_path=paths["trajectory_path"],
    )
    ep = store.open(
        opened_by="curiosity_gap", pressure={}, pipeline_id=0, work_kind="autonomous"
    )
    ep.global_ = {
        "action_goal": {"intent": "LEARN", "target": "Python"},
        "outcome": {
            "goal": {"intent": "LEARN", "target": "Python"},
            "outcome": {"goal_state": "completed", "status": "success"},
        },
    }
    ep.meta = {"execution_action_pack": {"action": "web_fetch"}}
    ep.auto = {"status": "success", "cycle_kind": "success"}

    # v2 rebuild: per-episode deepsleep is now _run_per_episode_terminus
    with pytest.raises(RuntimeError, match="mastery write failed"):
        cl._run_per_episode_terminus(store, ep, cascade_status="completed")


# --- MEDIUM-4: fold domain key clean ---


def _fold_setup(monkeypatch, *, questions: list[OpenQuestion]):
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {"target_domain": "attention mechanisms"},
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: questions,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_failures",
        lambda: {},
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.9,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        lambda question, failures, **kwargs: 0.0,
    )


def test_folded_trajectory_domain_is_clean_target_domain(monkeypatch):
    q = OpenQuestion(
        question_id="q-fold",
        curiosity_id="c1",
        act="follow_up_question",
        question_text="how do attention heads combine?",
        asked_cycle="cycle-1",
        asked_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        status=STATUS_OPEN,
        windows_survived=3,
    )
    _fold_setup(monkeypatch, questions=[q])

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert resolved.domain == _slug("attention mechanisms")
    assert "attention mechanisms:" in resolved.topic.lower()


# --- MEDIUM-5: blockage matches folded failure domain ---


def test_consolidate_failures_folded_target_uses_canonical_domain_id(tmp_path, monkeypatch):
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )
    scoped = "attention mechanisms: how do attention heads combine?"
    patterns = consolidate_failures(_failure_input("ep-fold-1", target=scoped))
    assert len(patterns) == 1
    assert patterns[0].domain_id == _slug("attention mechanisms")


def test_blockage_scores_folded_failure_domain_match():
    domain = _slug("attention mechanisms")
    scoped = "attention mechanisms: how do attention heads combine?"
    q = OpenQuestion(
        question_id="q1",
        curiosity_id="c1",
        act="follow_up_question",
        question_text="how do attention mechanisms work?",
        asked_cycle="cycle-1",
        asked_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        status=STATUS_OPEN,
    )
    failures = {
        "k1": FailurePattern(
            kind="tool",
            context=scoped,
            domain_id=domain,
            frequency=5,
            last_episode="ep-1",
        ),
    }
    assert _question_execution_blockage(q, failures) > 0.0


# --- MEDIUM-6: operator_query gated on operator origin ---


def test_autonomous_target_does_not_resolve_via_operator_query(monkeypatch):
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {"target_domain": "transformers"},
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

    state = make_meta_a_state(target_str="How do transformers work?")
    resolved = resolve_learning_topic(state)
    assert resolved.source != "operator_query"


def test_operator_flagged_target_resolves_via_operator_query():
    mi = MetaAInput(
        episode_id="ep-op",
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
            "episode_id": "ep-op",
            "intent": "LEARN",
            "target": "How do transformers work?",
            "confidence": 0.9,
            "reason": "operator_query",
        },
    )
    state = make_meta_a_state(target_str="How do transformers work?", mi=mi)
    resolved = resolve_learning_topic(state)
    assert resolved.source == "operator_query"
    assert resolved.topic == "transformers"
