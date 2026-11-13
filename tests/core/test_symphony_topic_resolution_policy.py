"""Item 5: the Symphony policy topic_resolution thresholds are loadable."""

from __future__ import annotations

from pathlib import Path

import yaml

from cognition.topic_resolution.resolve_topic import (
    SEVERE_MASTERY_GAP_THRESHOLD,
    _critical_open_question_thresholds,
    _severe_mastery_gap_threshold,
)


def test_symphony_yaml_declares_topic_resolution_thresholds():
    policy_path = Path(__file__).resolve().parents[2] / "policy" / "symphony.policy.yaml"
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    tr = raw["cognition"]["topic_resolution"]
    cq = tr["critical_open_question"]
    assert cq["persistence_threshold"] == 3
    assert cq["relevance_threshold"] == 0.6
    assert cq["blockage_threshold"] == 0.8
    assert cq["blockage_recency_decay"] == 0.85
    assert cq["blockage_recency_window"] == 20


def test_policy_loaders_match_symphony_defaults(monkeypatch):
    monkeypatch.setattr(
        "pressure_loop.policy.load_policy",
        lambda: {},
    )
    assert _severe_mastery_gap_threshold() == SEVERE_MASTERY_GAP_THRESHOLD
    thresholds = _critical_open_question_thresholds()
    assert thresholds["persistence_threshold"] == 3.0
    assert thresholds["relevance_threshold"] == 0.6
    assert thresholds["blockage_threshold"] == 0.8
