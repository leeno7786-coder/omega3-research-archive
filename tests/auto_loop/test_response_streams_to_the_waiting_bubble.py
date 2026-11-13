"""RISK 5 — the response stage streams into the bubble that is waiting.

The operator-facing reply is the one pass in the cascade whose latency is felt,
so it is the one that streams (operator 2026-08-11). Everything else in this
file is about making that change cost nothing:

  * the cascade still receives ONE whole ``response_text``. Streaming decides
    when the operator hears the answer, never what the answer is, and no stage
    after AUTO can tell which way it was delivered.
  * structured stages MUST NOT stream. JSON synthesis, tool_call dispatch and
    evaluation parse whole objects; a token at a time is not an object.
  * the stream addresses the same chat bubble the finished reply will —
    ``waiting_chat_turn`` is the single derivation, shared with
    ``cognition_chain._deliver_operator_chat_reply``.
  * ``OMEGA_RESPONSE_STREAM=0`` puts the reply back on the blocking call, and a
    stream that fails answers blocking without the operator noticing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from auto_loop._auto_state import MemoryEnvelope, PromptPack
from auto_loop.execute.run import execute
from pressure_loop.operator_chat_responses import reply_token_sink, waiting_chat_turn


# ── builders ─────────────────────────────────────────────────────────────────


def _prompt_pack(user: str = "how far is the moon") -> PromptPack:
    return PromptPack(
        prompt_type="RESPONSE_QUESTION",
        system_prompt="answer",
        developer_prompt=None,
        user_prompt=user,
        context_blocks={},
        response_mode=None,
        tool_call_spec=None,
        image_spec=None,
    )


def _empty_envelope() -> MemoryEnvelope:
    return MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )


class _Calls:
    """Records every /generate socket invocation the executor makes."""

    def __init__(self, results: Optional[List[Any]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._results = list(results or [])

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if self._results:
            return self._results.pop(0)
        return {"ok": True, "text": "the moon is 384,400 km away."}

    @property
    def response_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("operation") == "auto_response"]


def _run(
    calls: _Calls,
    *,
    chat_cycle_id: str = "",
    opened_by: str = "operator_query",
    pipeline_id: int = 0,
    work_kind: str = "operator_turn",
    job_id: str = "",
) -> Dict[str, Any]:
    with patch("runtime.socket.invoke_runtime_generate", calls):
        return execute(
            _prompt_pack(),
            "TEXT_FAST",
            {"action": "RESPONSE", "execution_inputs": {}},
            _empty_envelope(),
            "ep-1",
            runtime_client=object(),
            opened_by=opened_by,
            chat_cycle_id=chat_cycle_id,
            pipeline_id=pipeline_id,
            work_kind=work_kind,
            job_id=job_id,
        )


# ── the wiring ───────────────────────────────────────────────────────────────


def test_a_waiting_bubble_makes_the_reply_stream(monkeypatch):
    monkeypatch.delenv("OMEGA_RESPONSE_STREAM", raising=False)
    calls = _Calls()
    result = _run(calls, chat_cycle_id="cycle-7", pipeline_id=0)
    call = calls.response_calls[-1]
    assert call["method"] == "generate_completion_stream"
    assert callable(call["on_token"])
    # Streaming is a delivery choice, not semantic routing. The request keeps
    # the physical owner and its operator correlation.
    assert call["pipeline_id"] == 0
    assert call["work_kind"] == "operator_turn"
    assert call["cycle_id"] == "cycle-7"
    assert "job_id" not in call, "a direct answer has not opened a workspace job"
    # And the cascade still gets one whole reply.
    assert result["outputs"]["response_text"] == "the moon is 384,400 km away."


def test_no_waiting_bubble_means_no_stream(monkeypatch):
    """An autonomous episode has nobody watching. Nothing to stream to, so the
    pass runs exactly as it did before this cut."""
    monkeypatch.delenv("OMEGA_RESPONSE_STREAM", raising=False)
    calls = _Calls()
    _run(
        calls,
        chat_cycle_id="",
        opened_by="idle_wake",
        pipeline_id=3,
        work_kind="autonomous",
    )
    assert "method" not in calls.response_calls[-1]
    assert "on_token" not in calls.response_calls[-1]
    assert calls.response_calls[-1]["pipeline_id"] == 3
    assert calls.response_calls[-1]["work_kind"] == "autonomous"
    assert calls.response_calls[-1]["cycle_id"] == "ep-1"
    assert "job_id" not in calls.response_calls[-1]


def test_the_kill_switch_puts_the_reply_back_on_the_blocking_call(monkeypatch):
    monkeypatch.setenv("OMEGA_RESPONSE_STREAM", "0")
    calls = _Calls()
    result = _run(calls, chat_cycle_id="cycle-7")
    assert "method" not in calls.response_calls[-1]
    assert result["outputs"]["response_text"] == "the moon is 384,400 km away."


def test_a_failed_stream_is_reported_without_regeneration(monkeypatch):
    """A missing completion is an inference failure, never a second inference."""
    monkeypatch.delenv("OMEGA_RESPONSE_STREAM", raising=False)
    calls = _Calls(results=[None, {"ok": True, "text": "answered anyway."}])
    result = _run(calls, chat_cycle_id="cycle-7")
    assert len(calls.response_calls) == 1
    assert calls.response_calls[0]["method"] == "generate_completion_stream"
    assert result["failure_kind"]
    assert "stream_missing_result" in result["failure_reason"]


def test_a_client_that_cannot_stream_reports_the_actual_failure(monkeypatch):
    """An unavailable generation method cannot be mistaken for successful inference."""
    monkeypatch.delenv("OMEGA_RESPONSE_STREAM", raising=False)

    class _NoStream(_Calls):
        def __call__(self, **kwargs: Any) -> Any:
            if kwargs.get("method") == "generate_completion_stream":
                self.calls.append(dict(kwargs))
                raise RuntimeError("runtime client has no method")
            return super().__call__(**kwargs)

    calls = _NoStream()
    result = _run(calls, chat_cycle_id="cycle-7")
    assert "runtime client has no method" in result["outputs"]["response_text"]
    assert result["failure_kind"]
    assert len(calls.response_calls) == 1


def test_only_the_operator_reply_streams(monkeypatch):
    """Every OTHER pass the executor makes stays blocking. A structured stage
    parses a whole object, and a token at a time is not one."""
    monkeypatch.delenv("OMEGA_RESPONSE_STREAM", raising=False)
    calls = _Calls()
    _run(calls, chat_cycle_id="cycle-7")
    for call in calls.calls:
        if call.get("operation") != "auto_response":
            assert "method" not in call, f"{call.get('operation')} must not stream"


# ── one derivation of "which bubble is waiting" ──────────────────────────────


def test_waiting_chat_turn_finds_the_operator_cycle():
    pressure = {
        "contributors": [
            {"id": "operator_query", "detail": {"cycle_id": "c-1", "chat_bubble_waiting": True}}
        ]
    }
    assert waiting_chat_turn(pressure) == {"cycle_id": "c-1", "chat_bubble_waiting": True}


def test_an_autonomous_episode_owes_nobody_an_answer():
    assert waiting_chat_turn({"contributors": [{"id": "idle_wake", "detail": {}}]}) is None
    assert waiting_chat_turn({}) is None
    assert waiting_chat_turn(None) is None


def test_a_fast_reply_that_already_answered_leaves_no_bubble():
    # operator_reply_delivered means the inline rail answered — there is no
    # second bubble to fill and nothing to stream into.
    pressure = {
        "contributors": [
            {
                "id": "operator_query",
                "detail": {"cycle_id": "c-1", "operator_reply_delivered": True},
            }
        ]
    }
    assert waiting_chat_turn(pressure) is None


def test_a_browser_lane_episode_streams_into_the_original_chat_turn():
    pressure = {"contributors": [{
        "id": "lane_work_dispatch",
        "detail": {"lane_work_handoff": True, "original_cycle_id": "chat-9", "lane": "browser"},
    }]}
    assert waiting_chat_turn(pressure) == {"cycle_id": "chat-9", "chat_bubble_waiting": True}


def test_an_operator_episode_with_no_cycle_id_is_still_an_operator_episode():
    """It reports cycle_id="" rather than None: a RESOLVE_QUERY answer with no
    chat bubble behind it still has somewhere to go (the proactive pinboard),
    and collapsing this case into None would retire that delivery path."""
    pressure = {"contributors": [{"id": "operator_query", "detail": {}}]}
    assert waiting_chat_turn(pressure) == {"cycle_id": "", "chat_bubble_waiting": False}


# ── the sink ─────────────────────────────────────────────────────────────────


def test_the_sink_numbers_its_deltas_and_restarts_on_a_retry():
    sent: List[Dict[str, Any]] = []
    with patch("hud.chat_socket.publish_delta", lambda **kw: sent.append(kw) or 1):
        sink = reply_token_sink("c-9", "ep-9")
        sink("a")
        sink("b")
        sink.restart()
        sink("A")
    assert [(s["seq"], s["attempt"], s["text"]) for s in sent] == [
        (0, 0, "a"),
        (1, 0, "b"),
        # seq back to 0 on attempt 1: the retry REPLACES the first attempt's
        # tokens — it is the same answer generated again, not a continuation.
        (0, 1, "A"),
    ]
    assert {s["cycle_id"] for s in sent} == {"c-9"}


def test_the_sink_never_raises_at_a_thought():
    def _boom(**kwargs: Any) -> int:
        raise RuntimeError("socket gone")

    with patch("hud.chat_socket.publish_delta", _boom):
        sink = reply_token_sink("c-9", "ep-9")
        sink("a")  # must not raise — delivery is not a dependency of thought


def test_a_delta_reaches_a_live_socket_on_the_replys_own_cycle():
    from hud import chat_socket

    got: List[Dict[str, Any]] = []
    token = chat_socket.subscribe(got.append)
    try:
        chat_socket.publish_delta(cycle_id="c-3", episode_id="ep-3", text="hi", seq=0)
        # Blank text and a nameless cycle are no-ops, not empty frames.
        chat_socket.publish_delta(cycle_id="", episode_id="ep-3", text="x", seq=1)
        chat_socket.publish_delta(cycle_id="c-3", episode_id="ep-3", text="", seq=1)
    finally:
        chat_socket.unsubscribe(token)
    assert got == [
        {
            "kind": "chat.reply_delta",
            "cycle_id": "c-3",
            "episode_id": "ep-3",
            "text": "hi",
            "seq": 0,
            "attempt": 0,
            "event_seq": got[0]["event_seq"],
        }
    ]
