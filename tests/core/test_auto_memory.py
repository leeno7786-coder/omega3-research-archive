"""Slice 3 B5 — MRIL recall via memory.api.read_memory + exact lobes."""

from __future__ import annotations

import pytest

from auto_loop.auto import (
    MemoryEnvelope,
    denied_lobes_for_scope,
    invoke_remember_tool,
    memory_envelope_before_execute,
    memory_recall_required,
    route_memory_tool,
    route_memory_tools,
    run_auto_front,
    run_forced_memory_tools,
    scope_to_lobes,
)


def _pack(**kw):
    base = {
        "memory_required": True,
        "memory_reason": "prior statements",
        "memory_query": "What do you remember about my Python project?",
        "memory_delivery_mode": "single_hop_system",
        "memory_tools": (route_memory_tool("What do you remember about my Python project?"),),
        "memory_scope": ("coding", "workflows"),
        "tool": None,
        "source_action_goal": {"intent": "RESPOND", "target": "q"},
    }
    base.update(kw)
    return base


class _Candidate:
    def __init__(self):
        self.id = "mem_1"
        self.coord = [8, 0, 0]
        self.score = 0.7
        self.rerank_score = 0.9
        self.provenance = {"timestamp": "2026-01-01"}
        # text must lexically match the tests' queries ("Python project ...") or the
        # relevance floor (_partition_responsive, Gate-1 spec) correctly blocks a
        # non-matching candidate from the answer context — a real recall WOULD match.
        self.summary = "Python project files"
        self.title = "title"


class _Result:
    def __init__(self, candidates):
        self.candidates = candidates


def test_remember_code_reads_exact_lobe_via_memory_api():
    def fake_read(spec):
        assert spec.allowed_lobes == [8]
        assert spec.denied_lobes == [4, 6, 11]  # situational now denied for non-situational lanes
        return _Result([_Candidate()])

    out = invoke_remember_tool(
        {
            "tool": "remember_code",
            "query": "Python project layout",
            "allowed_lobes": [8],
            "memory_type": "episodic",
        },
        "ep_1",
        memory=fake_read,
    )
    assert out["lobe"] == 8
    assert out["lane"] == "coding"
    assert out["evidence"][0]["remembered_as"] == "Python project files"


def test_forced_tools_aggregate_envelope():
    def fake_read(spec):
        return _Result([_Candidate()] if spec.allowed_lobes == [8] else [])

    tool = route_memory_tool("What do you remember about my Python project?")
    pack = _pack(memory_tools=(tool,))
    results, env = run_forced_memory_tools(pack, "ep_1", memory=fake_read)
    assert len(results) == 1
    assert env.memory_status == "success"
    assert env.items


def test_recall_read_failure_is_not_soft():
    def boom(spec):
        raise RuntimeError("recall down")

    with pytest.raises(RuntimeError, match="recall down"):
        run_forced_memory_tools(_pack(), "ep_1", memory=boom)


def test_mril_read_failure_fails_episode():
    class Ep:
        episode_id = "ep_mr"
        pressure = {}
        global_ = {"action_goal": {"intent": "RESPOND", "episode_id": "ep_mr"}}
        meta = {
            "execution_action_pack": {
                "episode_id": "ep_mr",
                "action": "RESPONSE",
                "tool": None,
                "tool_reason": None,
                "memory_required": True,
                "memory_reason": "need",
                "memory_query": "remember my earlier message",
                "memory_delivery_mode": "single_hop_system",
                "memory_tools": (route_memory_tool("remember my earlier message"),),
                "memory_scope": ("persona", "situational"),
                "execution_intent": "x",
                "execution_inputs": {},
                "execution_constraints": {
                    "planning_only": True,
                    "no_inference_in_meta": True,
                    "no_execution_in_meta": True,
                    "no_model_fields": True,
                },
                "source_action_goal": {"intent": "RESPOND", "episode_id": "ep_mr"},
            }
        }
        auto = None

    class FakeRuntime:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

        def generate_completion(self, **kw):
            return {"text": "unused", "tokens_in": 1, "tokens_out": 1}

    def fail_read(spec):
        raise OSError("unavailable")

    out = run_auto_front(Ep, runtime_client=FakeRuntime(), memory=fail_read)
    assert out.auto["failure_kind"] == "memory_continuity_substrate_invalid"


def test_not_required_skipped_envelope():
    env = memory_envelope_before_execute(_pack(memory_required=False))
    assert env.memory_status == "skipped"
    assert env.items == ()


def test_required_pending_before_execute():
    env = memory_envelope_before_execute(_pack())
    assert env.memory_status == "pending"
    assert env.items == ()


def test_scope_to_lobes_coding():
    assert scope_to_lobes(["coding"]) == [8]


def test_denied_mind_process_lobes():
    # situational (4), journal (6), dev (11) are mind-process lobes — denied for
    # user-facing recall by default so graph fan-out cannot leak them; each opts back
    # in only when its OWN lane is named in the scope.
    assert denied_lobes_for_scope(("coding",)) == [4, 6, 11]
    assert denied_lobes_for_scope(("persona",)) == [4, 6, 11]
    assert 4 not in denied_lobes_for_scope(("situational",))  # situational recall opts in
    assert 6 not in denied_lobes_for_scope(("journal",))
    assert 11 not in denied_lobes_for_scope(("dev",))


def test_memory_recall_required():
    assert memory_recall_required(_pack())
    assert not memory_recall_required(_pack(memory_required=False))


def test_browser_route_overrides_stale_memory_contract():
    pack = _pack(
        execution_inputs={"operator_route": "browser"},
        memory_required=True,
    )
    assert not memory_recall_required(pack)
    envelope = memory_envelope_before_execute(pack)
    assert envelope.memory_required is False
    assert envelope.memory_status == "skipped"


# ── Cut 1: DME trace receipts ride the AUTO memory carrier ──────────────────

_TRACE = {
    "type": "dme_activation_trace",
    "trace_id": "trace-1",
    "episode_id": "ep_1",
    "query_hash": "abc",
    "projection_generation": "snap-1",
    "backend": "numpy",
    "cue_receipt_id": None,
    "query_coordinates": [[8, 0, 1]],
    "candidates": [],
    "associations": [],
    "schema_version": "omega.dme_activation_trace.v1",
}


def test_memory_envelope_receipts_default_empty():
    envelope = MemoryEnvelope(
        memory_required=False,
        memory_status="not_required",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )
    assert envelope.receipts == ()
    # positional six-argument construction (live callers) still works
    assert MemoryEnvelope(False, "skipped", None, (), (), None).receipts == ()


def test_forced_tools_aggregate_only_nonempty_receipts_with_tool_context(monkeypatch):
    import auto_loop.mril_tools.run as mril_run

    outputs = iter(
        [
            {
                "kind": "remembered_knowledge",
                "tool": "remember_code",
                "lobe": 8,
                "verdict": "verified",
                "evidence": [
                    {"memory_id": "m1", "confidence": 0.9, "remembered_as": "Python project files"}
                ],
                "receipt": {"dme_activation_traces": [_TRACE]},
            },
            {
                "kind": "remembered_knowledge",
                "tool": "remember_workflow",
                "lobe": 10,
                "verdict": "verified",
                "evidence": [],
                "receipt": {},
            },
        ]
    )
    monkeypatch.setattr(mril_run, "invoke_remember_tool", lambda *_a, **_k: next(outputs))
    pack = _pack(
        memory_tools=(
            {"tool": "remember_code", "allowed_lobes": [8]},
            {"tool": "remember_workflow", "allowed_lobes": [10]},
        )
    )
    results, env = run_forced_memory_tools(pack, "ep_1", memory=lambda _s: None)
    assert len(results) == 2
    assert env.receipts == (
        {"tool": "remember_code", "lobe": 8, "dme_activation_traces": [_TRACE]},
    )
    # receipts never leak into engram provenance items
    assert all("dme_activation_traces" not in item for item in env.items)
    assert [item["memory_id"] for item in env.items] == ["m1"]


def test_fold_preserves_receipts_and_keeps_them_out_of_the_prompt():
    from auto_loop._auto_state import AutoInput
    from auto_loop.fold.run import _fold_episode_auto
    from auto_loop.promptpack.run import build_prompt_pack

    envelope = MemoryEnvelope(
        memory_required=True,
        memory_status="success",
        memory_reason="prior statements",
        memory_scope=("coding",),
        items=(
            {
                "memory_id": "m1",
                "source": "lobe:8",
                "timestamp": None,
                "relevance": 0.9,
                "content": "Python project files",
                "tool": "remember_code",
            },
        ),
        error=None,
        receipts=({"tool": "remember_code", "lobe": 8, "dme_activation_traces": [_TRACE]},),
    )
    pack = {"action": "RESPOND", "execution_inputs": {}}
    ai = AutoInput("ep_1", {}, {}, pack, {}, None)
    prompt = build_prompt_pack(pack, envelope)
    folded = _fold_episode_auto(
        ai,
        status="success",
        cycle_kind="success",
        memory_envelope=envelope,
        model_profile=None,
        model_id=None,
        prompt_pack=prompt,
        exec_result={"outputs": {}},
        runtime_ms=1,
    )
    assert folded["memory"]["items"] == list(envelope.items)
    assert folded["memory"]["receipts"] == list(envelope.receipts)
    assert "dme_activation_trace" not in repr(folded["prompt_pack"])
    assert "dme_activation_trace" not in (prompt.system_prompt or "")
    assert "dme_activation_trace" not in (prompt.user_prompt or "")
    assert "dme_activation_trace" not in repr(prompt.context_blocks)
    # and the whole prompt pack, serialized, carries no trace field at all
    assert "trace-1" not in repr(prompt)
