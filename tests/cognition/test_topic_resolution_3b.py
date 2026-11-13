"""Topic Resolution Phase 3b — LEARN always recalls on resolved topic (knowledge lane)."""

from __future__ import annotations

import pytest

from auto_loop.mril_tools import run_forced_memory_tools
from meta_loop.intake.run import MetaAInput
from meta_loop.meta_a import build_execution_action_pack, validate_execution_action_pack
from memory.recall_lanes import LANE_KNOWLEDGE


def _learn_mi(
    *,
    target: str = "",
    message: str = "",
    opened_by: str = "learning_debt",
    **extra_meta,
):
    contributors = []
    if message or extra_meta:
        meta = {"message": message, **extra_meta}
        contributors.append(
            {
                "source": "autonomous",
                "type": opened_by,
                "weight": 0.8,
                "metadata": meta,
            }
        )
    return MetaAInput(
        episode_id="ep-3b",
        pressure={"reason": opened_by, "contributors": contributors},
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep-3b",
            "intent": "LEARN",
            "target": target if target is not None else message,
            "confidence": 0.8,
            "reason": "autonomous learn",
        },
    )


def _patch_mastery_gap_attention(monkeypatch):
    from cognition.topic_resolution.resolve_topic import ResolvedTopic

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.resolve_learning_topic",
        lambda state: ResolvedTopic(
            topic="attention",
            domain="attention",
            source="mastery_gap",
            confidence=0.7,
        ),
    )


def test_learn_memory_tool_carries_resolved_domain(monkeypatch):
    """Resolved domain threads into MRIL tool spec for QuerySpec.domain."""
    _patch_mastery_gap_attention(monkeypatch)

    pack = build_execution_action_pack(_learn_mi(target="", message=""))
    assert pack.memory_tools[0].get("domain") == "attention"


def test_invoke_remember_tool_uses_domain_from_tool_spec():
    seen: dict[str, object] = {}

    def _fake_read(spec):
        seen["domain"] = getattr(spec, "domain", None)
        return type("R", (), {"candidates": []})()

    from auto_loop.mril_tools.remember import invoke_remember_tool

    invoke_remember_tool(
        {
            "tool": "remember_knowledge",
            "query": "attention",
            "domain": "attention",
            "allowed_lobes": [2],
            "memory_type": "episodic",
        },
        "ep-domain",
        memory=_fake_read,
    )
    assert seen["domain"] == "attention"


def test_learn_with_resolved_topic_always_recalls_no_prior_history_gate(monkeypatch):
    """Autonomous LEARN + resolved topic, no operator-history phrasing -> recall required."""
    _patch_mastery_gap_attention(monkeypatch)

    pack = build_execution_action_pack(_learn_mi(target="", message=""))
    assert pack.memory_required is True
    assert pack.memory_query == "attention"
    assert len(pack.memory_tools) == 1
    assert pack.memory_tools[0]["tool"] == "remember_knowledge"
    assert pack.memory_tools[0]["lane"] == LANE_KNOWLEDGE
    validate_execution_action_pack(pack)


def test_learn_memory_query_uses_resolved_topic_not_raw_message(monkeypatch):
    """Resolved topic wins over raw operator phrasing (e.g. 'how to' must not mis-route)."""
    from cognition.topic_resolution.resolve_topic import ResolvedTopic

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.resolve_learning_topic",
        lambda state: ResolvedTopic(
            topic="how to implement attention",
            domain="attention",
            source="operator_query",
            confidence=1.0,
        ),
    )

    pack = build_execution_action_pack(
        _learn_mi(
            target="how to implement attention",
            message="Learn how to implement attention",
            opened_by="operator_command",
        )
    )
    assert pack.memory_required is True
    assert pack.memory_query == "how to implement attention"
    assert pack.memory_query != "Learn how to implement attention"
    assert pack.memory_tools[0]["tool"] == "remember_knowledge"
    assert pack.memory_tools[0]["tool"] != "remember_procedure"


def test_learn_empty_recall_continues_without_failure():
    """Empty knowledge recall is valid — episode tooling must not treat it as failure."""

    class _Result:
        def __init__(self, candidates):
            self.candidates = candidates

    pack = {
        "memory_required": True,
        "memory_reason": "Recall prior knowledge on the learning target: attention",
        "memory_query": "attention",
        "memory_delivery_mode": "inference_tool",
        "memory_tools": (
            {
                "tool": "remember_knowledge",
                "query": "attention",
                "allowed_lobes": [2],
                "memory_type": "episodic",
                "lane": LANE_KNOWLEDGE,
                "lobe": 2,
            },
        ),
        "memory_scope": ("knowledge",),
        "source_action_goal": {"intent": "LEARN"},
    }

    results, env = run_forced_memory_tools(
        pack,
        "ep-empty-recall",
        memory=lambda spec: _Result([]),
    )
    assert len(results) == 1
    assert results[0]["memory_type"] == "none"
    assert results[0]["evidence"] == []
    assert env.memory_status == "success"
    assert env.error is None


def test_learn_no_resolved_topic_skips_forced_recall(monkeypatch):
    from cognition.topic_resolution.resolve_topic import NO_TOPIC_AVAILABLE, ResolvedTopic

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.resolve_learning_topic",
        lambda state: ResolvedTopic(
            topic=NO_TOPIC_AVAILABLE,
            domain="",
            source="none",
            confidence=0.0,
        ),
    )

    pack = build_execution_action_pack(_learn_mi(target="", message=""))
    assert pack.memory_required is False
    assert not pack.memory_tools


def test_learn_no_topic_prior_history_fallback_still_recalls(monkeypatch):
    """LEARN with no resolved topic but prior-history phrasing keeps operator-history recall."""
    from cognition.topic_resolution.resolve_topic import NO_TOPIC_AVAILABLE, ResolvedTopic

    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.resolve_learning_topic",
        lambda state: ResolvedTopic(
            topic=NO_TOPIC_AVAILABLE,
            domain="",
            source="none",
            confidence=0.0,
        ),
    )

    pack = build_execution_action_pack(
        _learn_mi(
            target="",
            message="remember what I told you about Python earlier",
            opened_by="operator_command",
        )
    )
    assert pack.action == "IDLE"
    assert pack.memory_required is True
    assert pack.memory_reason == "Learning target references prior operator history"
    assert len(pack.memory_tools) > 0


def test_respond_recall_behavior_unchanged():
    pack = build_execution_action_pack(
        MetaAInput(
            episode_id="ep-respond",
            pressure={
                "reason": "operator_command",
                "contributors": [
                    {
                        "source": "operator",
                        "type": "operator_command",
                        "weight": 1.0,
                        "metadata": {
                            "message": "What did I tell Omega about Python?",
                        },
                    }
                ],
            },
            trajectory_snapshot={},
            action_goal={
                "episode_id": "ep-respond",
                "intent": "RESPOND",
                "target": {"question": "What did I tell Omega about Python?"},
                "confidence": 0.9,
                "reason": "test",
            },
        )
    )
    assert pack.memory_required is True
    assert pack.memory_query
    assert len(pack.memory_tools) > 0
    validate_execution_action_pack(pack)


def test_use_tool_recall_behavior_unchanged():
    pack = build_execution_action_pack(
        MetaAInput(
            episode_id="ep-use-tool",
            pressure={
                "reason": "operator_command",
                "contributors": [
                    {
                        "source": "operator",
                        "type": "operator_command",
                        "weight": 1.0,
                        "metadata": {
                            "message": "remember what I told you about Python earlier",
                        },
                    }
                ],
            },
            trajectory_snapshot={},
            action_goal={
                "episode_id": "ep-use-tool",
                "intent": "USE_TOOL",
                "target": "remember what I told you about Python earlier",
                "confidence": 0.9,
                "reason": "test",
            },
        )
    )
    assert pack.action == "RESPONSE"
    assert pack.memory_required is True
    assert pack.memory_query
    assert len(pack.memory_tools) > 0
    validate_execution_action_pack(pack)
