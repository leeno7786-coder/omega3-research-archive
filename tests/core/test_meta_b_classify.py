"""Slice 4 B2 — Meta B consistency + disposition."""

from __future__ import annotations

import pytest

from meta_loop.meta_b import MetaBInput, check_consistency, derive_disposition


def _mbi(**kwargs):
    base = {
        "episode_id": "ep1",
        "pressure": {"reason": "x", "contributors": []},
        "source_action_goal": {
            "episode_id": "ep1",
            "intent": "USE_TOOL",
            "target": "t",
            "confidence": 0.5,
            "reason": "r",
        },
        "execution_pack": {
            "episode_id": "ep1",
            "action": "USE_TOOL",
            "tool": "web_fetch",
            "execution_intent": "learn",
        },
        "auto_outcome": {
            "episode_id": "ep1",
            "source_action_goal": {
                "episode_id": "ep1",
                "intent": "USE_TOOL",
                "target": "t",
                "confidence": 0.5,
                "reason": "r",
            },
            "execution_pack": {
                "episode_id": "ep1",
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "execution_intent": "learn",
            },
            "auto": {
                "cycle_kind": "success",
                "status": "success",
                "telemetry": {"warnings": []},
                "outputs": {},
            },
        },
    }
    base.update(kwargs)
    return MetaBInput(**base)


@pytest.mark.parametrize(
    "cycle_kind,disposition,status",
    [
        ("success", "completed", "success"),
        ("partial", "degraded", "partial"),
        ("fail", "failed", "failed"),
        ("skipped", "skipped", "skipped"),
        ("idle", "idle", "idle"),
    ],
)
def test_derive_disposition_cycle_kinds(cycle_kind, disposition, status):
    d, s = derive_disposition({"cycle_kind": cycle_kind})
    assert d == disposition
    assert s == status


def test_derive_disposition_missing_cycle_kind_invalid():
    d, s = derive_disposition({"status": "weird"})
    assert d == "invalid"
    assert s == "invalid"


def test_derive_disposition_legacy_execution_label_invalid():
    d, s = derive_disposition({"status": "success", "telemetry": {"cycle_kind": "execution"}})
    assert d == "invalid"
    assert s == "invalid"


def test_episode_id_mismatch_critical():
    mbi = _mbi(episode_id="ep1", auto_outcome=_mbi().auto_outcome | {"episode_id": "ep2"})
    cons, critical = check_consistency(mbi)
    assert critical is True
    assert any("episode_id" in r for r in cons["mismatch_reasons"])


def test_action_mismatch_critical():
    ao = dict(_mbi().auto_outcome)
    ao["execution_pack"] = dict(ao["execution_pack"])
    ao["execution_pack"]["action"] = "RESPONSE"
    mbi = _mbi(auto_outcome=ao)
    cons, critical = check_consistency(mbi)
    assert critical is True
    assert any("action mismatch" in r for r in cons["mismatch_reasons"])


def test_tool_mismatch_critical():
    ao = dict(_mbi().auto_outcome)
    ao["execution_pack"] = dict(ao["execution_pack"])
    ao["execution_pack"]["tool"] = "ocr_tool"
    mbi = _mbi(auto_outcome=ao)
    cons, critical = check_consistency(mbi)
    assert critical is True
    assert any("tool mismatch" in r for r in cons["mismatch_reasons"])


def test_consistency_has_no_goal_matches_key():
    # No goal in v2 -> no goal-identity consistency. Only pack + episode_id.
    cons, critical = check_consistency(_mbi())
    assert "goal_matches" not in cons
    assert "pack_matches" in cons
    assert critical is False
