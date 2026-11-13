"""Topic Resolution Phase 3c.2a — trajectory relevance, THE FOLD, policy thresholds."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cognition.topic_resolution.resolve_topic import (
    _critical_open_question_thresholds,
    _pick_critical_open_question,
    _question_execution_blockage,
    _question_trajectory_relevance,
    _resolve_trajectory,
    resolve_learning_topic,
)
from deepsleep_loop._deepsleep_state import FailurePattern
from knowledge_extractor.derive.run import _slug
from pressure_loop.open_questions import OpenQuestion, STATUS_OPEN
from tests.cognition.test_topic_resolution import make_meta_a_state


def _open_question(
    *,
    question_id: str = "q1",
    question_text: str = "how do attention mechanisms work?",
    status: str = STATUS_OPEN,
    windows_survived: int = 0,
    asked_window_id: str | None = None,
) -> OpenQuestion:
    asked = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    return OpenQuestion(
        question_id=question_id,
        curiosity_id="c1",
        act="follow_up_question",
        question_text=question_text,
        asked_cycle="cycle-1",
        asked_at=asked,
        status=status,
        asked_window_id=asked_window_id,
        windows_survived=windows_survived,
    )


# --- B1: trajectory_relevance ---


def test_relevance_perfect_match(monkeypatch):
    monkeypatch.setattr("memory.retina.similarity", lambda _left, _right: 1.0)

    q = _open_question(question_text="attention mechanisms")
    relevance = _question_trajectory_relevance(q, {"target_domain": "attention mechanisms"})
    assert relevance == pytest.approx(1.0)


def test_relevance_orthogonal(monkeypatch):
    monkeypatch.setattr("memory.retina.similarity", lambda _left, _right: 0.0)

    q = _open_question(question_text="unrelated topic")
    relevance = _question_trajectory_relevance(q, {"target_domain": "attention mechanisms"})
    assert relevance == pytest.approx(0.0)


def test_relevance_degraded_runtime(monkeypatch):
    monkeypatch.setattr("memory.retina.similarity", lambda _left, _right: None)

    q = _open_question()
    assert _question_trajectory_relevance(q, {"target_domain": "attention"}) == 0.0


def test_relevance_empty_question():
    q = _open_question(question_text="")
    assert _question_trajectory_relevance(q, {"target_domain": "attention"}) == 0.0


def test_relevance_empty_trajectory():
    q = _open_question()
    assert _question_trajectory_relevance(q, {}) == 0.0


# --- B2: policy thresholds ---


def test_thresholds_default_when_policy_missing(monkeypatch):
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_policy",
        lambda: {},
    )
    thresholds = _critical_open_question_thresholds()
    assert thresholds["persistence_threshold"] == pytest.approx(3.0)
    assert thresholds["relevance_threshold"] == pytest.approx(0.6)
    assert thresholds["blockage_threshold"] == pytest.approx(0.8)


def test_thresholds_override_from_policy(monkeypatch):
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_policy",
        lambda: {
            "cognition": {
                "topic_resolution": {
                    "critical_open_question": {
                        "persistence_threshold": 2,
                    }
                }
            }
        },
    )
    thresholds = _critical_open_question_thresholds()
    assert thresholds["persistence_threshold"] == pytest.approx(2.0)


def test_thresholds_invalid_policy_value(monkeypatch):
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_policy",
        lambda: {
            "cognition": {
                "topic_resolution": {
                    "critical_open_question": {
                        "relevance_threshold": "bad",
                    }
                }
            }
        },
    )
    thresholds = _critical_open_question_thresholds()
    assert thresholds["relevance_threshold"] == pytest.approx(0.6)


# --- B3: THE FOLD ---


def _fold_setup(monkeypatch, *, questions, trajectory=None):
    trajectory = trajectory or {"target_domain": "attention mechanisms"}

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: trajectory,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: questions,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_failures",
        lambda: {},
    )


def test_fold_returns_question_when_critical(monkeypatch):
    q = _open_question(
        question_text="how do attention heads combine?",
        windows_survived=3,
    )
    _fold_setup(monkeypatch, questions=[q])
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.8,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        lambda question, failures, **kwargs: 0.0,
    )

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert "attention mechanisms" in resolved.topic
    assert "how do attention heads combine?" in resolved.topic
    assert resolved.source == "trajectory_fold"
    assert resolved.confidence == pytest.approx(0.90)
    assert resolved.fold_question_id == "q1"


def test_fold_returns_plain_trajectory_when_no_critical_question(monkeypatch):
    _fold_setup(monkeypatch, questions=[])

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert resolved.topic == "attention mechanisms"
    assert resolved.confidence == pytest.approx(0.85)


def test_fold_skips_off_path_question(monkeypatch):
    q = _open_question(
        question_text="why does the moon shine?",
        windows_survived=5,
    )
    _fold_setup(monkeypatch, questions=[q])
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.2,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        lambda question, failures, **kwargs: 0.0,
    )

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert resolved.topic == "attention mechanisms"
    assert resolved.confidence == pytest.approx(0.85)


def test_fold_prefers_blockage_override(monkeypatch):
    q = _open_question(
        question_text="how do attention heads combine?",
        windows_survived=1,
    )
    _fold_setup(monkeypatch, questions=[q])
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.9,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        lambda question, failures, **kwargs: 0.85,
    )

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert "how do attention heads combine?" in resolved.topic
    assert resolved.confidence == pytest.approx(0.90)


def test_fold_ranks_multiple_critical_questions(monkeypatch):
    q_low = _open_question(
        question_id="q-low",
        question_text="lower priority question",
        windows_survived=3,
    )
    q_high = _open_question(
        question_id="q-high",
        question_text="higher priority question",
        windows_survived=5,
    )
    _fold_setup(monkeypatch, questions=[q_low, q_high])

    def _relevance(question, trajectory, **kwargs):
        return 0.8

    def _blockage(question, failures, **kwargs):
        return 0.9 if question.question_id == "q-high" else 0.0

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        _relevance,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        _blockage,
    )

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert "higher priority question" in resolved.topic


def test_fold_degraded_failure_store(monkeypatch):
    q = _open_question(
        question_text="how do attention heads combine?",
        windows_survived=3,
    )
    _fold_setup(monkeypatch, questions=[q])

    def _raise_failures():
        raise RuntimeError("failure store unavailable")

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_failures",
        _raise_failures,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.8,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        lambda question, failures, **kwargs: 0.0,
    )

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert "how do attention heads combine?" in resolved.topic


def test_fold_degraded_embed(monkeypatch):
    q = _open_question(
        question_text="why does the moon shine?",
        windows_survived=5,
    )
    _fold_setup(monkeypatch, questions=[q])
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.0,
    )

    resolved = _resolve_trajectory()
    assert resolved is not None
    assert resolved.topic == "attention mechanisms"
    assert resolved.confidence == pytest.approx(0.85)


# --- LOW sweep D1: windows_survived persistence boundary ---


def test_windows_survived_persistence_boundary_both_directions(monkeypatch):
    trajectory = {"target_domain": "attention mechanisms"}
    q_at_2 = _open_question(
        question_id="q-survived-2",
        question_text="how do attention heads combine?",
        windows_survived=2,
    )
    q_at_3 = _open_question(
        question_id="q-survived-3",
        question_text="how do attention heads combine?",
        windows_survived=3,
    )

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_failures",
        lambda: {},
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.7,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        lambda question, failures, **kwargs: 0.0,
    )

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: [q_at_2],
    )
    assert _pick_critical_open_question(trajectory) is None

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: [q_at_3],
    )
    picked = _pick_critical_open_question(trajectory)
    assert picked is not None
    assert picked.question_id == "q-survived-3"


# --- B6: execution_blockage scorer ---


_RECENCY_LIVE_TESTS = frozenset(
    {
        "test_blockage_recency_prefers_recent_episode",
    }
)


@pytest.fixture(autouse=True)
def _neutral_blockage_recency_unless_live(monkeypatch, request):
    if request.node.name in _RECENCY_LIVE_TESTS:
        return
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._blockage_recency_weights",
        lambda **kwargs: {},
    )


def test_blockage_exact_domain_match():
    domain = _slug("attention mechanisms")
    q = _open_question(question_text="how do attention mechanisms work?")
    failures = {
        "k1": FailurePattern(
            kind="tool",
            context="attention mechanisms",
            domain_id=domain,
            frequency=5,
            last_episode="ep-1",
        ),
        "k2": FailurePattern(
            kind="tool",
            context="attention mechanisms",
            domain_id=domain,
            frequency=5,
            last_episode="ep-2",
        ),
    }
    score = _question_execution_blockage(q, failures)
    assert score == pytest.approx(1.0)


def test_blockage_no_match():
    q = _open_question(question_text="how do attention mechanisms work?")
    failures = {
        "k1": FailurePattern(
            kind="tool",
            context="unrelated",
            domain_id="unrelated_topic",
            frequency=5,
            last_episode="ep-1",
        ),
    }
    assert _question_execution_blockage(q, failures) == 0.0


def test_blockage_caps_at_one():
    domain = _slug("attention mechanisms")
    q = _open_question(question_text="how do attention mechanisms work?")
    failures = {
        f"k{i}": FailurePattern(
            kind="tool",
            context="attention mechanisms",
            domain_id=domain,
            frequency=20,
            last_episode=f"ep-{i}",
        )
        for i in range(4)
    }
    assert _question_execution_blockage(q, failures) == pytest.approx(1.0)


def test_blockage_empty_failures():
    q = _open_question()
    assert _question_execution_blockage(q, {}) == 0.0


def test_blockage_legacy_pattern_falls_back_to_slug_context():
    q = _open_question(question_text="how do attention mechanisms work?")
    failures = {
        "k1": FailurePattern(
            kind="tool",
            context="Attention Mechanisms",
            domain_id="",
            frequency=10,
            last_episode="ep-1",
        ),
    }
    score = _question_execution_blockage(q, failures)
    assert score == pytest.approx(0.5)


def test_failure_pattern_from_dict_backfills_domain_id():
    pattern = FailurePattern.from_dict(
        {
            "kind": "tool",
            "context": "Attention Mechanisms",
            "frequency": 2,
            "last_episode": "ep-1",
        }
    )
    assert pattern.domain_id == _slug("Attention Mechanisms")


def test_blockage_recency_prefers_recent_episode(monkeypatch):
    domain = _slug("attention mechanisms")
    q = _open_question(question_text="how do attention mechanisms work?")

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._blockage_recency_weights",
        lambda **kwargs: {"ep-old": 0.85, "ep-new": 1.0, "__floor__": 0.0},
    )

    old_only = {
        "k1": FailurePattern(
            kind="tool",
            context="attention mechanisms",
            domain_id=domain,
            frequency=5,
            last_episode="ep-old",
        ),
    }
    new_only = {
        "k1": FailurePattern(
            kind="tool",
            context="attention mechanisms",
            domain_id=domain,
            frequency=5,
            last_episode="ep-new",
        ),
    }
    old_score = _question_execution_blockage(q, old_only)
    new_score = _question_execution_blockage(q, new_only)
    assert new_score > old_score


def test_publish_topic_fold_writes_shared_state(monkeypatch):
    from cognition.topic_resolution.resolve_topic import _publish_topic_fold

    writes: list[tuple[str, object]] = []

    class _SS:
        def write(self, key, value):
            writes.append((key, value))

    class _RT:
        shared_state = _SS()

    monkeypatch.setattr(
        "pressure_loop.runtime.try_get_default_runtime",
        lambda: _RT(),
    )
    q = _open_question(question_text="how do attention heads combine?")
    _publish_topic_fold(
        target_domain="attention mechanisms",
        question=q,
        scoped_topic="attention mechanisms: how do attention heads combine?",
    )
    assert writes
    assert writes[0][0] == "last_topic_fold"
    assert writes[0][1]["active"] is True
    assert writes[0][1]["question_id"] == "q1"


# --- B7: end-to-end fold with real persistence ---


def test_fold_end_to_end_with_real_windows_survived(monkeypatch):
    """windows_survived>=3 + relevance>=0.6 folds; off-path does not."""
    on_path = _open_question(
        question_text="how do attention heads combine?",
        windows_survived=3,
    )
    off_path = _open_question(
        question_id="q-off",
        question_text="why does the moon shine?",
        windows_survived=5,
    )

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.read_trajectory",
        lambda: {"target_domain": "attention mechanisms"},
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.list_open",
        lambda: [off_path, on_path],
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_failures",
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
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.now_iso",
        lambda: datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
    )

    def _relevance(question, trajectory, **kwargs):
        if "moon" in question.question_text.lower():
            return 0.2
        return 0.8

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        _relevance,
    )

    resolved = resolve_learning_topic(make_meta_a_state(target_str="", text=""))
    assert resolved.source == "trajectory_fold"
    assert "how do attention heads combine?" in resolved.topic
    assert "moon" not in resolved.topic.lower()
    assert resolved.confidence == pytest.approx(0.90)


def test_oscillation_guard_off_path_question_never_steers(monkeypatch):
    """A highly persistent question about an unrelated domain must NOT fold."""
    q = _open_question(
        question_text="why does the moon shine?",
        windows_survived=5,
    )
    _fold_setup(monkeypatch, questions=[q])
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_trajectory_relevance",
        lambda question, trajectory, **kwargs: 0.0,
    )
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic._question_execution_blockage",
        lambda question, failures, **kwargs: 0.0,
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
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.now_iso",
        lambda: datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
    )

    resolved = resolve_learning_topic(make_meta_a_state(target_str="", text=""))
    assert resolved.topic == "attention mechanisms"
    assert resolved.source == "trajectory"
    assert "moon" not in resolved.topic.lower()
