"""Trajectory DECOUPLED from pressure (operator 2026-06-30): the window's self-authored
trajectory must NOT raise pressure by default — it influences via the per-episode prompt
(foldback trajectory_influencer) + steers topic selection. The legacy reseed coupling
(trajectory_gap + next_inquiry curiosity) is opt-in via OMEGA_TRAJECTORY_PRESSURE."""

from __future__ import annotations

from deepsleep_loop._deepsleep_state import DeepSleepInput
from deepsleep_loop.reseed.run import generate_reseed


def _ds_input() -> DeepSleepInput:
    return DeepSleepInput(
        window_id="w1",
        episode_window=[],
        window_reflection={
            "reflection": {
                "trajectory": {
                    "next_learning_pressure": 0.8,
                    "next_inquiry_pressure": 0.6,
                    "target_domain": "ai",
                },
                "pressure_feedback": {},
            },
            "trajectory_update": {"decision": "establish", "target_domain": "ai"},
        },
        trajectory_snapshot={"target_domain": "ai"},
    )


def test_default_emits_no_trajectory_or_inquiry_pressure(monkeypatch):
    monkeypatch.delenv("OMEGA_TRAJECTORY_PRESSURE", raising=False)  # default = decoupled
    types = {c.type for c in generate_reseed(_ds_input())}
    assert "trajectory_gap" not in types  # the operator's win-condition
    assert "curiosity_gap" not in types  # next_inquiry no longer raises pressure


def test_flag_on_restores_legacy_trajectory_pressure(monkeypatch):
    monkeypatch.setenv("OMEGA_TRAJECTORY_PRESSURE", "1")
    types = {c.type for c in generate_reseed(_ds_input())}
    assert "trajectory_gap" in types
    assert "curiosity_gap" in types


def test_mastery_records_are_context_not_automatic_pressure(monkeypatch):
    # DeepSleep mastery is maintenance context for Omega. It cannot independently
    # ignite cognition; only Omega's explicit successor or enabled idle learning may.
    monkeypatch.delenv("OMEGA_TRAJECTORY_PRESSURE", raising=False)
    from deepsleep_loop._deepsleep_state import MasteryRecord

    rec = MasteryRecord(
        domain_id="800_coding/python",
        domain_name="python",
        knowledge_mastery=0.2,
        application_mastery=0.1,
        target_mastery=0.8,
    )
    types = {c.type for c in generate_reseed(_ds_input(), mastery_records=[rec])}
    assert "mastery_gap" not in types
    assert "trajectory_gap" not in types
