"""Slice 3 B7 — fold, emit, run_auto_front orchestration."""

from __future__ import annotations

import json

from auto_loop.auto import run_auto_front
from meta_loop.meta_a import run_meta_a_front


RESEARCH_URL = "https://example.invalid/python"
RESEARCH_CONTENT = "Python iterators provide items one at a time."


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def health_body(self):
        return {
            "status": "ok",
            "capabilities": {"coding_model": True, "image_generation": True},
        }

    def generate_completion(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            assert kw.get("tools")
            return {"text": "", "tool_calls": [{
                "id": "read-python", "type": "function",
                "function": {"name": "web_fetch",
                             "arguments": json.dumps({"query": RESEARCH_URL})},
            }]}
        assert len(self.calls) == 2
        results = [turn for turn in kw.get("messages") or () if turn.get("role") == "tool"]
        assert len(results) == 1 and results[0]["tool_call_id"] == "read-python"
        body = str(results[0]["content"] or "")
        assert RESEARCH_CONTENT in body
        assert '<external-data trust="untrusted-human-authored">' in body
        return {"text": "synthetic reply", "tool_calls": []}

    def image_generate(self, **kw):
        return b"\x00\x01"


def _fake_web_fetch(name, args):
    assert name == "web_fetch" and args == {"query": RESEARCH_URL}
    return {"ok": True, "elapsed_ms": 2, "result": {
        "query": RESEARCH_URL,
        "results": [{"url": RESEARCH_URL, "title": "Python iterators",
                     "content": RESEARCH_CONTENT, "source": "example.invalid"}],
    }}


def _episode_learn():
    class Ep:
        episode_id = "ep_fold"
        opened_by = "operator_command"
        pressure = {
            "opened_by": "operator_command",
            "contributors": [
                {
                    "source": "operator",
                    "type": "operator_command",
                    "weight": 1.0,
                    "metadata": {"message": "Learn Python"},
                }
            ],
        }
        global_ = {
            "action_goal": {
                "episode_id": "ep_fold",
                "intent": "LEARN",
                "target": "Python",
                "confidence": 0.9,
                "reason": "t",
            }
        }
        meta = {}
        auto = None

    run_meta_a_front(Ep)
    return Ep


def test_run_writes_only_episode_auto():
    ep = _episode_learn()
    meta_before = dict(ep.meta)
    global_before = dict(ep.global_)

    out = run_auto_front(ep, runtime_client=FakeRuntime(), tool_invoker=_fake_web_fetch)
    assert out is not None
    assert out.auto["status"] == "success"
    assert ep.auto
    assert ep.meta == meta_before
    assert ep.global_ == global_before
    assert "omega" not in dir(ep) or getattr(ep, "omega", None) in (None, {})


def test_auto_outcome_pack_shape_and_echoes():
    ep = _episode_learn()

    runtime = FakeRuntime()
    out = run_auto_front(ep, runtime_client=runtime, tool_invoker=_fake_web_fetch)
    assert out.episode_id == "ep_fold"
    assert out.source_action_goal["intent"] == "LEARN"
    assert out.execution_pack["tool"] == "web_fetch"
    assert out.auto["status"] == "success"
    assert out.auto["cycle_kind"] == "success"
    assert out.auto["telemetry"]["runtime_ms"] >= 0
    assert ep.auto["prompt_pack"]["system_prompt_hash"]
    assert len(runtime.calls) == 2
    assert ep.auto["outputs"]["tool_result"]["results"][0]["content"] == RESEARCH_CONTENT


def test_failure_invalid_pack():
    class Ep:
        episode_id = "ep_bad"
        pressure = {}
        global_ = {}
        meta = {
            "execution_action_pack": {
                "episode_id": "ep_bad",
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "tool_reason": None,
                "memory_required": False,
                "memory_reason": None,
                "memory_scope": [],
                "execution_intent": "x",
                "execution_inputs": {},
                "execution_constraints": {},
                "source_action_goal": {"episode_id": "ep_bad"},
            }
        }
        auto = None

    out = run_auto_front(Ep, runtime_client=FakeRuntime())
    assert out.auto["status"] == "failed"
    assert out.auto["cycle_kind"] == "fail"
    assert out.auto["failure_kind"] == "invalid_execution_pack"
