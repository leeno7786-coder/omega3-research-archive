"""Lifecycle demotion after governed contradiction penalty."""

from __future__ import annotations

import pytest

from memory.demotion import (
    demote_one_lifecycle_stage,
    demotion_trigger_after_contradiction,
    count_contradiction_strikes,
)


def test_demote_semantic_fact_to_concept():
    doc = {
        "id": "2.00.abc",
        "type": "semantic_fact",
        "flags": {},
        "audit": [],
    }
    result = demote_one_lifecycle_stage(doc, reason="test")
    assert result["demoted"] is True
    assert result["type_before"] == "semantic_fact"
    assert result["type_after"] == "concept"
    assert doc["type"] == "concept"
    assert doc["audit"][-1]["what"] == "lifecycle_demoted"


def test_demote_skips_canonical():
    doc = {
        "id": "2.00.canon",
        "type": "semantic_fact",
        "flags": {"canonical": True},
    }
    result = demote_one_lifecycle_stage(doc, reason="test")
    assert result["demoted"] is False
    assert result["reason"] == "canonical_immune"
    assert doc["type"] == "semantic_fact"


def test_count_contradiction_strikes_from_audit():
    doc = {
        "audit": [
            {"what": "ingested"},
            {"what": "contradiction_adjudicated"},
            {"what": "lifecycle_demoted"},
            {"what": "contradiction_adjudicated"},
        ]
    }
    assert count_contradiction_strikes(doc) == 2


def test_demotion_trigger_at_strike_threshold():
    doc = {
        "audit": [
            {"what": "contradiction_adjudicated"},
            {"what": "contradiction_adjudicated"},
        ]
    }
    should, reason = demotion_trigger_after_contradiction(doc, confidence_after=0.50)
    assert should is True
    assert reason == "contradiction_strike_threshold"


def test_demotion_trigger_not_at_single_strike():
    doc = {"audit": [{"what": "contradiction_adjudicated"}]}
    should, reason = demotion_trigger_after_contradiction(doc, confidence_after=0.50)
    assert should is False
    assert reason == ""


def test_demote_floor_at_observation():
    doc = {"id": "2.00.obs", "type": "observation", "audit": []}
    result = demote_one_lifecycle_stage(doc, reason="test")
    assert result["demoted"] is False
    assert result["reason"] == "already_floor"
