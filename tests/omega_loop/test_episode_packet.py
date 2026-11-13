"""Slice 1 — OmegaEpisodeInput fold packet (RED-first)."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from deepsleep_loop._deepsleep_state import MasteryRecord
from cognition_chain import _operator_request_text
from global_loop.global_b import run_global_b_front
from meta_loop.meta_b import run_meta_b_front
from omega_loop.episode.types import OmegaEpisodeInput


def _hermetic_episode(
    *,
    intent: str = "LEARN",
    action: str = "USE_TOOL",
    tool: str = "web_fetch",
    execution_inputs: dict | None = None,
    auto_payload: dict | None = None,
    eid: str = "ep-pkt-1",
):
    inputs = execution_inputs or {
        "query": "quantum physics",
        "domain": "quantum_physics",
    }
    auto = auto_payload or {
        "cycle_kind": "success",
        "memory_status": "success",
        "memory": {
            "memory_required": True,
            "memory_status": "success",
            "memory_reason": "learn recall",
            "memory_scope": ["general"],
            "items": [],
            "error": None,
        },
        "outputs": {
            "tool_result": {"results": [{"title": "Quantum basics", "snippet": "..."}]},
            "remembered_knowledge_tool_results": [
                {
                    "kind": "remembered_knowledge",
                    "tool": "remember_knowledge",
                    "memory_type": "semantic",
                    "confidence": 0.8,
                    "evidence": [{"text": "prior fact"}],
                }
            ],
        },
        "telemetry": {"warnings": []},
    }
    episode = SimpleNamespace(
        episode_id=eid,
        pressure={"opened_by": "operator_command", "contributors": []},
        global_={
            "intent_decision": {
                "episode_id": eid,
                "intent": intent,
                "confidence": 0.9,
                "reason": "test",
            }
        },
        meta={
            "execution_action_pack": {
                "episode_id": eid,
                "action": action,
                "tool": tool,
                "execution_intent": "plan",
                "execution_inputs": dict(inputs),
                "source_action_goal": {
                    "episode_id": eid,
                    "intent": intent,
                    "confidence": 0.9,
                    "reason": "test",
                },
            }
        },
        auto=auto,
    )
    episode.auto = {
        "episode_id": eid,
        "action": action,
        "source_action_goal": deepcopy(episode.global_["intent_decision"]),
        "execution_pack": deepcopy(episode.meta["execution_action_pack"]),
        **deepcopy(auto),
    }
    return episode


def _build_packet(ep, monkeypatch, *, mastery_store: dict | None = None):
    if mastery_store is not None:
        monkeypatch.setattr(
            "omega_loop.episode.packet.load_mastery",
            lambda path=None: dict(mastery_store),
        )

    def _boom(*_a, **_k):
        raise AssertionError("invoke_runtime_generate must not run for packet build")

    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        _boom,
        raising=False,
    )
    run_meta_b_front(ep)
    run_global_b_front(ep, ep.meta["reflection"])
    raw = (ep.global_ or {}).get("omega_packet")
    assert raw is not None, "episode.global_['omega_packet'] missing"
    if raw.get("prior_mastery") is not None:
        from omega_loop.episode.types import MasteryView

        raw = dict(raw)
        raw["prior_mastery"] = MasteryView(**raw["prior_mastery"])
    raw = dict(raw)
    raw["recall"] = tuple(raw.get("recall") or ())
    return OmegaEpisodeInput(**raw)


def test_packet_carries_raw_tool_result_recall_outcome_domain_target(monkeypatch):
    store = {
        "quantum-physics": MasteryRecord(
            domain_id="quantum-physics",
            domain_name="quantum_physics",
            knowledge_mastery=0.2,
            application_mastery=0.1,
            target_mastery=0.7,
        )
    }
    ep = _hermetic_episode()
    pkt = _build_packet(ep, monkeypatch, mastery_store=store)

    assert pkt.raw_tool_result == {"results": [{"title": "Quantum basics", "snippet": "..."}]}
    assert len(pkt.recall) == 1
    assert pkt.recall[0]["kind"] == "remembered_knowledge"
    assert pkt.outcome == "success"
    assert pkt.domain == "quantum_physics"
    assert pkt.prior_mastery is not None
    assert pkt.prior_mastery.target_mastery == 0.7
    assert pkt.prior_mastery.knowledge_mastery == 0.2


def test_packet_carries_onboarding_context_only_from_bootstrap_contributor(monkeypatch):
    ep = _hermetic_episode()
    context = {
        "occupation": "software_engineer",
        "categories": [
            {"title": "Python", "lobe": 8, "y": 0, "memory_id": "mem-python"},
            {"title": "System Design", "lobe": 2, "y": 0, "memory_id": "mem-design"},
        ],
        "source_citation": {"source_id": "src-onboarding"},
    }
    ep.pressure["contributors"] = [
        {
            "type": "operator_command",
            "detail": {
                "origin": "onboarding_bootstrap",
                "message": "Learn Python",
                "onboarding_context": context,
            },
        }
    ]

    packet = _build_packet(ep, monkeypatch)

    assert packet.onboarding_context == context


def test_empty_recall_is_valid_packet_builds(monkeypatch):
    ep = _hermetic_episode(
        auto_payload={
            "cycle_kind": "success",
            "memory_status": "skipped",
            "memory": {
                "memory_required": False,
                "memory_status": "skipped",
                "memory_reason": "",
                "memory_scope": [],
                "items": [],
                "error": None,
            },
            "outputs": {
                "tool_result": {"results": []},
                "remembered_knowledge_tool_results": [],
            },
            "telemetry": {"warnings": []},
        }
    )
    pkt = _build_packet(ep, monkeypatch, mastery_store={})
    assert pkt.recall == ()
    assert pkt.raw_tool_result == {"results": []}
    assert pkt.outcome == "success"


def test_partial_execution_status_reaches_omega_packet(monkeypatch):
    ep = _hermetic_episode(
        intent="USE_SKILL",
        action="USE_SKILL",
        tool=None,
        execution_inputs={"objective": "finish the build"},
        auto_payload={
            "cycle_kind": "partial",
            "status": "partial",
            "failure_kind": "skill_yielded",
            "failure_reason": "checkpoint reached",
            "memory_status": "skipped",
            "outputs": {},
            "telemetry": {"warnings": []},
        },
    )
    pkt = _build_packet(ep, monkeypatch, mastery_store={})
    assert pkt.outcome == "partial"


def test_recall_error_propagates_into_packet(monkeypatch):
    ep = _hermetic_episode(
        auto_payload={
            "cycle_kind": "fail",
            "failure_kind": "memory_continuity_substrate_invalid",
            "failure_reason": "read_memory timeout",
            "memory_status": "error",
            "memory": {
                "memory_required": True,
                "memory_status": "error",
                "memory_reason": "recall failed",
                "memory_scope": ["general"],
                "items": [],
                "error": "read_memory timeout",
            },
            "outputs": {"tool_result": None},
            "telemetry": {"warnings": []},
        }
    )
    pkt = _build_packet(ep, monkeypatch, mastery_store={})
    assert pkt.recall_error == "read_memory timeout"
    assert pkt.outcome == "failure"
    assert pkt.failure_reason == "read_memory timeout"


def test_learn_vs_response_action_and_operator_text(monkeypatch):
    learn_ep = _hermetic_episode(intent="LEARN", action="USE_TOOL")
    learn_pkt = _build_packet(learn_ep, monkeypatch, mastery_store={})
    assert learn_pkt.intent == "LEARN"
    assert learn_pkt.action == "USE_TOOL"
    assert learn_pkt.operator_text is None

    respond_ep = _hermetic_episode(
        intent="RESPOND",
        action="RESPONSE",
        tool=None,
        execution_inputs={"query": "What is Python?"},
        auto_payload={
            "cycle_kind": "success",
            "memory_status": "success",
            "memory": {
                "memory_required": True,
                "memory_status": "success",
                "memory_reason": "respond recall",
                "memory_scope": ["general"],
                "items": [],
                "error": None,
            },
            "outputs": {
                "response_text": "Python is a language.",
                "remembered_knowledge_tool_results": [],
            },
            "telemetry": {"warnings": []},
        },
    )
    respond_ep.meta["execution_action_pack"]["tool"] = None
    respond_pkt = _build_packet(respond_ep, monkeypatch, mastery_store={})
    assert respond_pkt.intent == "RESPOND"
    assert respond_pkt.action == "RESPONSE"
    assert respond_pkt.operator_text == "What is Python?"
    assert respond_pkt.raw_tool_result is None


def test_ask_operator_seed_from_pressure_contributors(monkeypatch):
    ask_ep = _hermetic_episode(
        intent="ASK_OPERATOR",
        action="ASK_OPERATOR",
        tool=None,
        execution_inputs={"_test": "no_operator_text_here"},
    )
    ask_ep.pressure = {
        "opened_by": "operator_priority",
        "contributors": [
            {
                "id": "operator_priority",
                "weight": 0.9,
                "detail": {
                    "question": "What should I prioritize learning?",
                    "message": "What should I prioritize learning?",
                },
            }
        ],
    }
    ask_pkt = _build_packet(ask_ep, monkeypatch, mastery_store={})
    assert ask_pkt.intent == "ASK_OPERATOR"
    assert ask_pkt.action == "ASK_OPERATOR"
    assert ask_pkt.operator_text == "What should I prioritize learning?"


def test_response_operator_text_ignores_front_inserted_rollback_event(monkeypatch):
    respond_ep = _hermetic_episode(
        intent="RESPOND",
        action="RESPONSE",
        tool=None,
        execution_inputs={"query": "fallback query"},
        auto_payload={
            "cycle_kind": "success",
            "memory_status": "skipped",
            "memory": {
                "memory_required": False,
                "memory_status": "skipped",
                "memory_reason": "",
                "memory_scope": [],
                "items": [],
                "error": None,
            },
            "outputs": {"response_text": "working", "remembered_knowledge_tool_results": []},
            "telemetry": {"warnings": []},
        },
    )
    respond_ep.meta["execution_action_pack"]["tool"] = None
    respond_ep.pressure = {
        "opened_by": "operator_command",
        "contributors": [
            {
                "id": "dev_rollback",
                "weight": 1.0,
                "detail": {"message": "rolled back F:/workspace/app.py"},
            },
            {
                "id": "operator_command",
                "weight": 1.0,
                "detail": {"message": "build the dashboard"},
            },
        ],
    }
    pkt = _build_packet(respond_ep, monkeypatch, mastery_store={})
    assert pkt.operator_text == "build the dashboard"


def test_ask_operator_concern_ignores_front_inserted_rollback_event(monkeypatch):
    ask_ep = _hermetic_episode(
        intent="ASK_OPERATOR",
        action="ASK_OPERATOR",
        tool=None,
        execution_inputs={"_test": "no_operator_text_here"},
    )
    ask_ep.pressure = {
        "opened_by": "clarification_gap",
        "contributors": [
            {
                "id": "dev_rollback",
                "weight": 1.0,
                "detail": {"message": "rolled back F:/workspace/app.py"},
            },
            {
                "id": "clarification_gap",
                "weight": 0.9,
                "detail": {
                    "question": "Need the target framework before continuing.",
                },
            },
        ],
    }
    ask_pkt = _build_packet(ask_ep, monkeypatch, mastery_store={})
    assert ask_pkt.operator_text == "Need the target framework before continuing."


def test_build_request_text_ignores_front_inserted_rollback_event():
    episode = SimpleNamespace(
        pressure={
            "contributors": [
                {
                    "id": "dev_rollback",
                    "detail": {"message": "rolled back F:/workspace/app.py"},
                },
                {
                    "id": "operator_command",
                    "detail": {"message": "build the dashboard"},
                },
            ]
        }
    )
    assert _operator_request_text(episode) == "build the dashboard"


def test_packet_carries_dev_rollback_events_from_episode_meta(monkeypatch):
    ep = _hermetic_episode()
    ep.meta["dev_rollback_events"] = [
        {
            "type": "dev_rollback",
            "snapshot_id": "snap-omega",
            "file_path": "F:/workspace/app.py",
            "action": "restore",
            "message": "rolled back F:/workspace/app.py",
        }
    ]
    pkt = _build_packet(ep, monkeypatch, mastery_store={})
    assert pkt.rollback_events == (
        {
            "type": "dev_rollback",
            "snapshot_id": "snap-omega",
            "file_path": "F:/workspace/app.py",
            "action": "restore",
            "message": "rolled back F:/workspace/app.py",
        },
    )


def test_packet_carries_plugin_result_as_external_tool_evidence(monkeypatch):
    ep = _hermetic_episode()
    ep.auto["outputs"].update(
        {
            "plugin_plan": {
                "plan_id": "plan-1",
                "authority_digest": "a" * 64,
            },
            "plugin_result": {
                "plugin_id": "plugin.synthetic",
                "app_id": "app.synthetic",
                "capability": "lookup",
                "status": "completed",
                "data": {"value": "observed"},
            },
        }
    )
    pkt = _build_packet(ep, monkeypatch, mastery_store={})

    assert pkt.raw_tool_result["plugin_id"] == "plugin.synthetic"
    assert pkt.foldback_packet["plugin_execution"]["plan_id"] == "plan-1"
