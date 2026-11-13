"""Slice 4 B1 — Meta B types + input assembly."""

from __future__ import annotations

from meta_loop.meta_b import (
    DISPOSITIONS,
    META_STATUSES,
    MetaBInput,
    MetaReflectionPack,
    meta_b_input_from_episode,
)
from global_loop.global_b import GlobalBInput, global_b_input_from_episode


def test_vocab_exact():
    assert DISPOSITIONS == ("completed", "degraded", "failed", "skipped", "idle", "invalid")
    assert META_STATUSES == ("success", "partial", "failed", "skipped", "idle", "invalid")


def _slice3_episode(*, auto_payload=None, goal=None, pack=None, eid="ep_b1"):
    class Ep:
        episode_id = eid
        opened_by = "operator_command"
        pressure = {"opened_by": "operator_command", "contributors": []}
        global_ = {
            "action_goal": goal
            or {
                "episode_id": eid,
                "intent": "USE_TOOL",
                "target": "Python",
                "confidence": 0.9,
                "reason": "learn",
            }
        }
        meta = {
            "execution_action_pack": pack
            or {
                "episode_id": eid,
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "execution_intent": "learn",
                "source_action_goal": goal
                or {
                    "episode_id": eid,
                    "intent": "USE_TOOL",
                    "target": "Python",
                    "confidence": 0.9,
                    "reason": "learn",
                },
            }
        }
        auto = None

    ep = Ep()
    ep.auto = auto_payload
    return ep


def test_meta_b_input_assembles_from_slice3_episode():
    ep = _slice3_episode(
        auto_payload={
            "episode_id": "ep_b1",
            "status": "success",
            "cycle_kind": "success",
            "telemetry": {"cycle_kind": "success", "runtime_ms": 10, "warnings": []},
            "outputs": {"tool_result": {"ok": True}},
            "memory": {"memory_status": "skipped"},
        }
    )
    mbi = meta_b_input_from_episode(ep)
    assert mbi is not None
    assert isinstance(mbi, MetaBInput)
    assert mbi.episode_id == "ep_b1"
    assert mbi.source_action_goal["intent"] == "USE_TOOL"
    assert mbi.execution_pack["tool"] == "web_fetch"
    assert mbi.auto_outcome["auto"]["status"] == "success"


def test_meta_b_input_none_without_auto():
    ep = _slice3_episode(auto_payload=None)
    assert meta_b_input_from_episode(ep) is None


def test_global_b_input_from_episode():
    ep = _slice3_episode(
        auto_payload={
            "episode_id": "ep_b1",
            "status": "success",
            "telemetry": {"cycle_kind": "success", "runtime_ms": 5},
            "outputs": {},
        }
    )
    reflection = {"episode_id": "ep_b1", "consistency": {}, "execution": {}, "trace_summary": {}}
    gbi = global_b_input_from_episode(ep, reflection)
    assert isinstance(gbi, GlobalBInput)
    assert gbi.meta_reflection == reflection


def test_meta_reflection_pack_dataclass():
    pack = MetaReflectionPack(
        episode_id="e1",
        consistency={"goal_matches": True, "pack_matches": True, "mismatch_reasons": []},
        execution={"action": "IDLE", "tool": None, "status": "skipped", "disposition": "skipped"},
        trace_summary={
            "has_response_text": False,
            "has_tool_result": False,
            "has_image_artifact": False,
            "runtime_ms": 0,
        },
    )
    assert pack.episode_id == "e1"
