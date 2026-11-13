"""Memory contradiction adjudication reads governed config."""

from __future__ import annotations

import pytest

from config import loader
from core import memory_contradiction as mc
from core import memory_config as mcfg


@pytest.fixture(autouse=True)
def _reset_memory_config_cache():
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()
    yield
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()


def _doc(*, mid: str, confidence: float, evidence: int = 0, when: str = ""):
    return {
        "id": mid,
        "provenance": {"confidence": confidence},
        "evidence_refs": ["e"] * evidence,
        "written_at": when,
    }


def test_contradiction_rules_from_bundle():
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()
    rules = mcfg.contradiction_rules()
    live = loader.load_subsystem_bundle("memory")["config"]["memory"]["contradiction"]
    assert rules["resolution"] == live["resolution"]
    assert rules["loser_confidence_penalty"] == live["loser_confidence_penalty"]


def test_adjudicate_prefers_higher_confidence():
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()
    result = mc.adjudicate_contradiction_pair(
        _doc(mid="a", confidence=0.8),
        _doc(mid="b", confidence=0.4),
    )
    assert result["winner_id"] == "a"
    assert result["loser_id"] == "b"
    assert result["loser_penalty"] == mcfg.contradiction_rules()["loser_confidence_penalty"]


def test_adjudicate_breaks_confidence_tie_by_evidence():
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()
    result = mc.adjudicate_contradiction_pair(
        _doc(mid="a", confidence=0.6, evidence=1),
        _doc(mid="b", confidence=0.6, evidence=3),
    )
    assert result["winner_id"] == "b"


def test_adjudicate_breaks_full_tie_by_recency():
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()
    result = mc.adjudicate_contradiction_pair(
        _doc(mid="a", confidence=0.6, evidence=1, when="2026-01-01T00:00:00Z"),
        _doc(mid="b", confidence=0.6, evidence=1, when="2026-06-01T00:00:00Z"),
    )
    assert result["winner_id"] == "b"
