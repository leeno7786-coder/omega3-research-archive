"""Governance — retired config surfaces must not reappear."""

from __future__ import annotations

import inspect

import yaml

from config import loader
from core.paths import OMEGA_ROOT

RETIRED_LOADER_NAMES = frozenset(
    {
        "load_queue_config",
        "load_failure_config",
        "load_goals_config",
        "load_trait_config",
        "load_autonomous_loop_config",
        "load_brain_loop_config",
        "load_meta_config",
        "load_global_config",
        "load_phase_registry_config",
        "load_phase_clock_config",
        "load_omega_intervention_config",
        "load_system_config",
        "load_dialogue_acts_config",
        "load_affect_attribution_config",
        "load_temporal_grounding_config",
        "load_plasticity_config",
        "load_stress_config",
    }
)

RETIRED_GLOBAL_KEYS = frozenset(
    {
        "extract",
        "goal",
        "goal_state",
        "global_b",
        "ground",
        "summarize",
        "followups",
        "outcome_pack",
    }
)

RETIRED_PRESSURE_CONTRIBUTOR_IDS = frozenset(
    {"evidence_terminal", "tool_creation_need", "guardian"}
)


def test_retired_loader_functions_absent():
    live = {name for name, _ in inspect.getmembers(loader, inspect.isfunction)}
    present = RETIRED_LOADER_NAMES & live
    assert not present, f"retired loaders must not be re-added: {sorted(present)}"


def test_removed_config_policy_schema_surfaces_stay_deleted():
    for rel in (
        "config/temporal_grounding.yaml",
        "config/plasticity.yaml",
        "config/stress.yaml",
        "policy/temporal_grounding.policy.yaml",
        "policy/plasticity.policy.yaml",
        "policy/stress.policy.yaml",
        "schema/temporal_grounding.schema.yaml",
        "schema/temporal_grounding.py",
        "schema/phase_clock.py",
        "schema/plasticity.schema.yaml",
        "schema/stress.schema.yaml",
        "schema/shared_state.schema.json",
    ):
        assert not (OMEGA_ROOT / rel).exists(), f"retired surface {rel} must stay deleted"


def test_global_yaml_is_v2_only():
    doc = yaml.safe_load((OMEGA_ROOT / "config" / "global.yaml").read_text(encoding="utf-8"))
    g = doc["global"]
    assert g["global_a"]["episode_write"]["slot"] == "intent_decision"
    assert g["global_a"]["phases"] == ["ignition", "route", "intent"]
    assert RETIRED_GLOBAL_KEYS.isdisjoint(g.keys())


def test_meta_yaml_requires_intent_decision_not_action_goal():
    meta = yaml.safe_load((OMEGA_ROOT / "config" / "meta.yaml").read_text(encoding="utf-8"))
    requires = meta["meta"]["meta_a"]["requires"]
    assert "episode.global_.intent_decision" in requires
    assert "episode.global_.action_goal" not in requires


def test_pressure_contributors_yaml_has_no_retired_active_blocks():
    doc = yaml.safe_load(
        (OMEGA_ROOT / "config" / "pressure_contributors.yaml").read_text(encoding="utf-8")
    )
    contributors = doc["pressure_contributors"]["contributors"]
    assert RETIRED_PRESSURE_CONTRIBUTOR_IDS.isdisjoint(contributors.keys())
    retired = doc["pressure_contributors"]["retired_contributors"]
    assert RETIRED_PRESSURE_CONTRIBUTOR_IDS <= set(retired.keys())


def test_pressure_schema_has_no_action_goal_type():
    doc = yaml.safe_load(
        (OMEGA_ROOT / "schema" / "pressure.schema.yaml").read_text(encoding="utf-8")
    )
    assert "ActionGoal" not in doc.get("definitions", {})


def test_global_extract_max_target_chars_removed():
    import pressure_loop.policy as pol

    assert not hasattr(pol, "global_extract_max_target_chars")


def test_auto_config_does_not_reintroduce_retired_python_skill_bridge():
    auto = yaml.safe_load((OMEGA_ROOT / "config" / "auto.yaml").read_text(encoding="utf-8"))
    skill_execution = auto["auto"].get("skill_execution") or {}
    assert "runtime.qwen_cli_client" not in yaml.safe_dump(skill_execution)
    assert "qwen_only_from_auto" not in skill_execution
