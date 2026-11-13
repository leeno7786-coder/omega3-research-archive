"""Terminal cognition slice 2 — meta action/tool + auto route/execute emits."""

import pytest
from unittest.mock import patch

from auto_loop.loop import run_auto
from meta_loop.loop import run_meta_a


@pytest.fixture(autouse=True)
def mock_list_open(monkeypatch):
    monkeypatch.setattr("cognition.topic_resolution.resolve_topic.list_open", lambda: [])


def _collect_events():
    events = []

    def _cb(ev):
        events.append(ev)

    return events, patch("guardian.terminal_feedback._publish_event", side_effect=_cb)


def _phase_ok(events, *, loop: str, phase: str):
    return [
        e
        for e in events
        if e.get("kind") == "phase"
        and e.get("loop") == loop
        and e.get("phase") == phase
        and e.get("status") == "ok"
    ]


def _meta_episode():
    class Ep:
        episode_id = "ep-meta-term"
        pressure = {
            "opened_by": "curiosity_gap",
            "contributors": [
                {
                    "source": "autonomous",
                    "type": "curiosity_gap",
                    "weight": 0.9,
                    "metadata": {
                        "domain": "ai/transformers",
                        "message": "learn transformers",
                    },
                }
            ],
        }
        global_ = {
            "action_goal": {
                "episode_id": "ep-meta-term",
                "intent": "LEARN",
                "target": "transformers",
                "confidence": 0.9,
                "reason": "test",
            }
        }
        meta = {}

    return Ep()


def test_meta_action_phase_carries_action():
    events, ctx = _collect_events()
    with ctx:
        run_meta_a(_meta_episode())

    ok = _phase_ok(events, loop="meta", phase="action")
    assert len(ok) == 1
    assert ok[0]["action"] == "USE_TOOL"


def test_meta_tool_phase_carries_tool_and_domain():
    events, ctx = _collect_events()
    with ctx:
        run_meta_a(_meta_episode())

    ok = _phase_ok(events, loop="meta", phase="tool")
    assert len(ok) == 1
    assert ok[0]["tool"] == "web_fetch"
    assert ok[0]["domain"] == "ai-transformers"


def _auto_episode_with_domain():
    class Ep:
        episode_id = "ep-auto-term"
        opened_by = "curiosity_gap"
        pressure = {
            "opened_by": "curiosity_gap",
            "contributors": [],
        }
        global_ = {
            "action_goal": {
                "episode_id": "ep-auto-term",
                "intent": "LEARN",
                "target": "transformers",
                "confidence": 0.9,
                "reason": "test",
            }
        }
        meta = {
            "execution_action_pack": {
                "episode_id": "ep-auto-term",
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "tool_reason": "Acquire knowledge on topic: transformers",
                "memory_required": False,
                "memory_reason": None,
                "memory_scope": [],
                "execution_intent": "Acquire knowledge inputs for learning",
                "execution_inputs": {
                    "query": "transformers",
                    "domain": "ai/transformers",
                },
                "execution_constraints": {
                    "planning_only": True,
                    "no_inference_in_meta": True,
                    "no_execution_in_meta": True,
                    "no_model_fields": True,
                },
                "source_action_goal": {
                    "episode_id": "ep-auto-term",
                    "intent": "LEARN",
                    "target": "transformers",
                    "confidence": 0.9,
                    "reason": "test",
                },
            }
        }
        auto = None

    return Ep()


def test_auto_route_phase_carries_tool_and_domain():
    events, ctx = _collect_events()
    exec_result = {
        "outputs": {"tool_result": "ok"},
        "failure_kind": None,
        "failure_reason": None,
        "model_id": "CODING_MODEL",
        "warnings": [],
        "tokens": None,
        "inference_latency_s": None,
    }

    def fake_tool(name, args):
        return {"ok": True, "result": {"q": args.get("query")}, "elapsed_ms": 2}

    with (
        ctx,
        patch(
            "auto_loop.loop.execute_phase.execute",
            return_value=exec_result,
        ),
        patch("auto_loop.recall.run.memory_envelope_before_execute") as recall,
    ):
        from auto_loop._auto_state import MemoryEnvelope

        recall.return_value = MemoryEnvelope(
            memory_required=False,
            memory_status="skipped",
            memory_reason=None,
            memory_scope=(),
            items=(),
            error=None,
        )
        run_auto(_auto_episode_with_domain(), tool_invoker=fake_tool)

    ok = _phase_ok(events, loop="auto", phase="route")
    assert len(ok) == 1
    assert ok[0]["tool"] == "web_fetch"
    assert ok[0]["domain"] == "ai/transformers"


def test_auto_execute_phase_carries_tokens_and_tok_s():
    events, ctx = _collect_events()
    exec_result = {
        "outputs": {"tool_result": "ok", "tool_result_summary": "summary"},
        "failure_kind": None,
        "failure_reason": None,
        "model_id": "CODING_MODEL",
        "warnings": [],
        "tokens": 1658,
        "inference_latency_s": 55.27,
    }

    def fake_tool(name, args):
        return {"ok": True, "result": {"q": args.get("query")}, "elapsed_ms": 2}

    with (
        ctx,
        patch(
            "auto_loop.loop.execute_phase.execute",
            return_value=exec_result,
        ),
        patch("auto_loop.recall.run.memory_envelope_before_execute") as recall,
    ):
        from auto_loop._auto_state import MemoryEnvelope

        recall.return_value = MemoryEnvelope(
            memory_required=False,
            memory_status="skipped",
            memory_reason=None,
            memory_scope=(),
            items=(),
            error=None,
        )
        run_auto(_auto_episode_with_domain(), tool_invoker=fake_tool)

    ok = _phase_ok(events, loop="auto", phase="execute")
    assert len(ok) == 1
    assert ok[0]["tool"] == "web_fetch"
    assert ok[0]["domain"] == "ai/transformers"
    assert ok[0]["tokens"] == 1658
    assert ok[0]["tok_s"] == 30.0


def test_auto_execute_idle_skips_token_fields():
    events, ctx = _collect_events()

    class Ep:
        episode_id = "ep-idle"
        pressure = {}
        global_ = {}
        meta = {
            "execution_action_pack": {
                "episode_id": "ep-idle",
                "action": "IDLE",
                "tool": None,
                "tool_reason": None,
                "memory_required": False,
                "memory_reason": None,
                "memory_scope": [],
                "execution_intent": "idle",
                "execution_inputs": {},
                "execution_constraints": {
                    "planning_only": True,
                    "no_inference_in_meta": True,
                    "no_execution_in_meta": True,
                    "no_model_fields": True,
                },
                "source_action_goal": {"episode_id": "ep-idle"},
            }
        }
        auto = None

    with ctx:
        run_auto(Ep)

    ok = _phase_ok(events, loop="auto", phase="execute")
    assert len(ok) == 1
    assert "tokens" not in ok[0]
    assert "tok_s" not in ok[0]
