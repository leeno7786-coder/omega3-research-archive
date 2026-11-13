"""Governance — Phase 7 integrations align with config/policy/schema mirrors."""

from __future__ import annotations

import yaml

from core.paths import OMEGA_ROOT


def _load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pressure_schema_includes_taxonomy_source_and_types():
    doc = _load_yaml(OMEGA_ROOT / "schema" / "pressure.schema.yaml")
    source_enum = doc["definitions"]["contributor_source"]["enum"]
    type_enum = doc["definitions"]["contributor_type"]["enum"]
    assert "memory_taxonomy" in source_enum
    assert "recall_verdict" in source_enum
    for kind in (
        "capability_gap",
        "repair_needed",
        "verification_needed",
        "requires_operator_input",
        "memory_gap",
        "sense_resolution",
        "memory_reconciliation",
    ):
        assert kind in type_enum


def test_global_intent_yaml_maps_taxonomy_pressure_to_intents():
    gi = _load_yaml(OMEGA_ROOT / "config" / "global_intent.yaml")["global_intent"]
    m = gi["opened_by_map"]
    assert m["capability_gap"] == "USE_SKILL"
    assert m["verification_needed"] == "USE_SKILL"
    assert m["requires_operator_input"] == "ASK_OPERATOR"
    assert m["memory_gap"] == "LEARN"
    assert m["sense_resolution"] == "IDLE"
    assert m["memory_reconciliation"] == "IDLE"
    assert "ASK_OPERATOR" in gi["allowed"]


def test_global_intent_code_maps_taxonomy_opened_by():
    from global_loop.intent.run import _map_opened_by_to_intent

    assert _map_opened_by_to_intent("capability_gap", "") == "USE_SKILL"
    assert _map_opened_by_to_intent("verification_needed", "") == "USE_SKILL"
    assert _map_opened_by_to_intent("requires_operator_input", "") == "ASK_OPERATOR"
    assert _map_opened_by_to_intent("confidence_reinforcement", "") == "LEARN"
    assert _map_opened_by_to_intent("memory_gap", "") == "LEARN"
    assert _map_opened_by_to_intent("sense_resolution", "") == "IDLE"
    assert _map_opened_by_to_intent("memory_reconciliation", "") == "IDLE"


def test_pressure_policy_documents_taxonomy_seam():
    pol = _load_yaml(OMEGA_ROOT / "policy" / "pressure.policy.yaml")
    seam = pol["taxonomy_advisory_seam"]
    assert seam["flag_env"] == "OMEGA_MEMORY_TAXONOMY_PRESSURE"
    assert "memory_taxonomy" in seam["description"]


def test_cognition_chain_policy_documents_taxonomy_capture():
    pol = _load_yaml(OMEGA_ROOT / "policy" / "cognition_chain.policy.yaml")
    cap = pol["memory_taxonomy_capture"]
    assert cap["module"] == "deepsleep_loop.loop.run_deepsleep_for_episode"
    assert "reindexes synchronously" in cap["description"]


def test_meta_schema_defines_skill_recall_context():
    doc = _load_yaml(OMEGA_ROOT / "schema" / "meta.schema.yaml")
    assert "SkillRecallContext" in doc["definitions"]
    assert "TaxonomyCompileReport" in doc["definitions"]
