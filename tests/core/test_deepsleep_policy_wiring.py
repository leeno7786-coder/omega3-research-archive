"""DeepSleep reads store paths and thresholds via pressure_loop.policy."""

from __future__ import annotations

from pressure_loop.policy import (
    clear_policy_cache,
    deepsleep_mastery_status_thresholds,
    deepsleep_semantic_developing_threshold,
    deepsleep_semantic_promote_evidence,
    deepsleep_trajectory_decay_after,
)
from deepsleep_loop.mastery_store import status_for


def test_deepsleep_semantic_thresholds_from_config():
    clear_policy_cache()
    assert deepsleep_semantic_promote_evidence() == 3
    assert deepsleep_semantic_developing_threshold() == 0.40


def test_deepsleep_mastery_status_from_config():
    clear_policy_cache()
    thresholds = deepsleep_mastery_status_thresholds()
    assert thresholds["learning_max"] == 0.60
    assert status_for(0.50) == "learning"
    assert status_for(0.65) == "operational"
    assert status_for(0.85) == "mastered"


def test_deepsleep_trajectory_decay_from_config():
    clear_policy_cache()
    assert deepsleep_trajectory_decay_after() == 10
