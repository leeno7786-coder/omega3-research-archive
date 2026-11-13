"""Acceptance: one LEARN episode closes through its DeepSleep terminus."""

from __future__ import annotations

import json
from types import SimpleNamespace

from deepsleep_loop.journal_store import read_journal
from deepsleep_loop.loop import run_deepsleep_for_episode
from memory import episode_sram
from pressure_loop import pressure_field
from pressure_loop.episode import EpisodeStore
from tests.core.deepsleep_window_fixtures import (
    fixture_omega_result,
    patch_window_deepsleep_paths,
)


def _reflection_json() -> str:
    return json.dumps(
        {
            "reasoning": {
                "action": "Chose LEARN on Quantum Physics, opened by curiosity pressure",
                "outcome": "tool-assisted web_fetch succeeded; cascade ok",
                "assessment": "right action for the trajectory",
                "next_move": "continue toward quantum mastery",
                "identity": "curious autonomous learner",
            },
            "episode_assessment": {
                "outcome_alignment": 0.92,
                "trajectory_alignment": 0.88,
                "novelty_score": 0.55,
                "confidence": 0.9,
            },
            "pattern_analysis": {
                "recurring_patterns": ["LEARN episodes on physics"],
                "successful_patterns": ["domain-targeted web_fetch succeeds"],
                "failed_patterns": [],
                "anomalies": [],
            },
            "emergent_observations": {
                "repeated_behaviors": ["LEARN on physics"],
                "stable_patterns": [],
                "unresolved_patterns": [],
            },
            "trajectory": {
                "target_domain": "quantum_physics",
                "target_capability": "foundational understanding",
                "confidence": 0.85,
                "rationale": "Successful learning episode",
                "evidence": ["outcome success"],
                "continuation_recommended": True,
                "next_learning_pressure": 0.65,
                "next_inquiry_pressure": 0.4,
            },
            "open_question_detected": True,
            "open_questions": [
                {
                    "question": "How does quantum uncertainty relate to information theory?",
                    "domain": "quantum_physics",
                    "confidence": 0.81,
                }
            ],
            "pressure_feedback": {
                "contributors_to_raise": [],
                "contributors_to_lower": [],
                "contributors_to_create": [],
            },
        }
    )


def test_deepsleep_acceptance_learn_episode_closes_and_persists_journal(tmp_path, monkeypatch):
    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)

    store = EpisodeStore()
    ep = store.open(
        opened_by="curiosity_gap",
        pressure={"opened_by": "curiosity_gap"},
        pipeline_id=0,
        work_kind="autonomous",
    )
    assert ep is not None
    episode_sram.open_bucket(ep.episode_id, pipeline_id=ep.pipeline_id)

    reflection = json.loads(_reflection_json())
    episode = SimpleNamespace(
        episode_id=ep.episode_id,
        global_={
            "action_goal": {
                "intent": "LEARN",
                "target": "Quantum Physics",
                "confidence": 0.9,
                "reason": "curiosity",
            },
            "outcome": {
                "goal": {"intent": "LEARN", "target": "Quantum Physics"},
                "outcome": {"goal_state": "completed", "summary": "success"},
                "observations": {"response_text": True, "memory_used": False},
                "artifacts": {"response_text": "Quantum basics summary"},
                "followups": [],
                "consistency": {},
            },
            "omega_result": fixture_omega_result(
                intent="LEARN",
                target="quantum_physics",
                success=True,
                knowledge_mastery=0.5,
                application_mastery=0.0,
                knowledge_products=None,
            ),
        },
        meta={
            "execution_action_pack": {"action": "web_fetch", "target": "Quantum Physics"},
            # The verdict the real Meta B writes for a successful cycle (WO-06 reads it at close).
            "reflection": {"execution": {"action": "web_fetch", "executed": True,
                                         "result": "success", "status": "success",
                                         "disposition": "completed"}},
        },
        auto={"status": "success", "cycle_kind": "success"},
        omega={
            "reflection": reflection,
            "trajectory_update": {
                "target_domain": "quantum_physics",
                "target_capability": "foundational understanding",
                "confidence": 0.85,
                "rationale": "Successful learning episode",
                "supporting_episode": ep.episode_id,
            },
        },
    )
    ep.global_ = episode.global_
    ep.meta = episode.meta
    ep.auto = episode.auto
    ep.omega = episode.omega
    outcome = run_deepsleep_for_episode(store)

    assert store.current() is None
    assert outcome is not None
    assert outcome.episode_closed is True
    assert outcome.pressure_reset is True

    acc = float(pressure_field._PRESSURE_ACCUMULATOR["accumulated"])
    assert acc == 0.0

    journal = read_journal(1, path=paths["deepsleep_journal"])
    assert len(journal) == 1
    assert journal[0].intent == "LEARN"
    assert "quantum-physics" in journal[0].mastery_effects
    assert journal[0].query_effects == []

    assert outcome.journal_entry.get("intent") == "LEARN"
    assert outcome.updated_memory == outcome.journal_entry
    assert outcome.updated_open_queries == {}
    assert outcome.projection_state
    assert outcome.pressure_seed == []
