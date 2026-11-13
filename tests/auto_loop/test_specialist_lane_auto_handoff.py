"""AUTO acknowledges a Global A specialist route only after the lane handoff is durable.

AGENTS.md §4.1 and §7.1: an operator turn enters the Chat lane; when Global A routes it to
Browser or the Coding Agent, Meta A carries ``execution_inputs.specialist_handoff`` and AUTO
admits that work to its target lane FIFO through
``pressure_loop.specialist_lane_handoff.stage_operator_lane_work`` BEFORE it acknowledges in
Chat. The acknowledgement is an AUTO action result — deterministic, no model pass.

Pinned here through the REAL ``auto_loop.execute.run.execute``:

1. a valid Browser / Coding Agent handoff stages exactly one lane-work item carrying the
   lane, request, original chat cycle, thread, route context — and Global A's bound browser
   workflow on the Browser lane only;
2. the reply exists only after admission returned; a failed admission yields no reply and
   keeps its cause;
3. no runtime inference runs on the handoff path;
4. a handoff on a non-Chat pipeline, or on an episode no operator turn opened, is refused
   without enqueue.

The helper is replaced on its own module, so production must resolve it from
``pressure_loop.specialist_lane_handoff`` at call time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from auto_loop._auto_state import AutoInput, MemoryEnvelope, PromptPack
from auto_loop.execute.run import execute
from auto_loop.fold.run import _fold_episode_auto, auto_outcome_from_fold
from pressure_loop import specialist_lane_handoff

CHAT_PIPELINE = 0
CYCLE = "operator-cycle"
THREAD = "conversation-1"
REQUEST = "Find current pricing"
ROUTE_CONTEXT = "the current request"
WORKFLOW = {"workflow_id": "workflow-1", "objective": REQUEST, "status": "active"}


def _handoff(lane: str) -> dict:
    handoff = {
        "lane": lane,
        "original_cycle_id": CYCLE,
        "thread_id": THREAD,
        "request": REQUEST,
        "route_context": ROUTE_CONTEXT,
    }
    if lane == "browser":
        handoff["browser_workflow"] = dict(WORKFLOW)
    if lane == "agent":
        handoff["selected_skill"] = "repo-helper"
    return handoff


def _prompt_pack() -> PromptPack:
    return PromptPack(prompt_type="RESPONSE_QUESTION", system_prompt="answer",
                      developer_prompt=None, user_prompt=REQUEST, context_blocks={},
                      response_mode=None, tool_call_spec=None, image_spec=None)


def _envelope() -> MemoryEnvelope:
    return MemoryEnvelope(memory_required=False, memory_status="skipped", memory_reason=None,
                          memory_scope=(), items=(), error=None)


class _NoInference:
    """Any model pass on the handoff path is a defect."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        pytest.fail("runtime inference ran on a specialist handoff")


class _NoRuntimeClient:
    def __getattr__(self, name):
        pytest.fail(f"the runtime client was used on a specialist handoff: {name}")


class _Stager:
    """Stands in for ``stage_operator_lane_work``; records each admission it performs."""

    def __init__(self, *, raises: BaseException | None = None):
        self.calls: list[dict] = []
        self.raises = raises

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        return {
            "status": "queued",
            "handoff_id": "h" * 64,
            "lane": kwargs["lane"],
            "original_cycle_id": kwargs["cycle_id"],
            "thread_id": kwargs["thread_id"],
            "origin_ref": "operator_cycle:" + kwargs["cycle_id"],
        }


def _run(monkeypatch, lane: str, *, stager: _Stager, pipeline_id: int = CHAT_PIPELINE,
         opened_by: str = "operator_query", handoff: dict | None = None):
    monkeypatch.setattr(specialist_lane_handoff, "stage_operator_lane_work", stager)
    inference = _NoInference()
    pack = {
        "action": "RESPONSE",
        "execution_inputs": {
            "specialist_handoff": handoff if handoff is not None else _handoff(lane),
            "operator_route": lane,
            "operator_route_context": ROUTE_CONTEXT,
        },
    }
    with patch("runtime.socket.invoke_runtime_generate", inference):
        result = execute(_prompt_pack(), "TEXT_FAST", pack, _envelope(), "chat-episode",
                         runtime_client=_NoRuntimeClient(), opened_by=opened_by,
                         chat_cycle_id=CYCLE, pipeline_id=pipeline_id, work_kind="chat",
                         thread_id=THREAD, request_cycle_id=CYCLE)
    return result, inference


# ── 1. a valid handoff is admitted to its lane ────────────────────────────────

@pytest.mark.parametrize("lane", ["browser", "agent"])
def test_a_valid_handoff_stages_its_lane_work_with_the_full_route(lane, monkeypatch):
    stager = _Stager()

    result, inference = _run(monkeypatch, lane, stager=stager)

    expected = {
        "lane": lane,
        "request": REQUEST,
        "cycle_id": CYCLE,
        "thread_id": THREAD,
        "route_context": ROUTE_CONTEXT,
        "arrival_seq": 0,
    }
    if lane == "browser":
        expected["browser_workflow"] = WORKFLOW
    else:
        expected["selected_skill"] = "repo-helper"
    [call] = stager.calls
    # The Coding Agent carries no browser workflow: absent or explicitly None, never a value.
    if lane == "agent":
        assert call.pop("browser_workflow", None) is None
    assert call == expected
    assert result["failure_kind"] is None
    assert inference.calls == []


# ── 2. the reply follows admission ────────────────────────────────────────────

@pytest.mark.parametrize("lane", ["browser", "agent"])
def test_the_acknowledgement_is_deterministic_and_follows_admission(lane, monkeypatch):
    first, _ = _run(monkeypatch, lane, stager=_Stager())
    second, _ = _run(monkeypatch, lane, stager=_Stager())

    text = first["outputs"].get("response_text")
    assert isinstance(text, str) and text.strip(), "AUTO's acknowledgement resolves the turn"
    assert second["outputs"]["response_text"] == text, "no model pass: the same receipt, same words"


@pytest.mark.parametrize("lane", ["browser", "agent"])
def test_a_failed_admission_yields_no_acknowledgement_and_keeps_its_cause(lane, monkeypatch):
    cause = RuntimeError("lane FIFO unwritable")
    stager = _Stager(raises=cause)

    try:
        result, inference = _run(monkeypatch, lane, stager=stager)
    except RuntimeError as raised:
        assert raised is cause, "the queue failure propagates unchanged"
        return

    assert len(stager.calls) == 1
    assert result["failure_kind"], "could-not-admit never shares a return with success"
    assert "lane FIFO unwritable" in str(result.get("failure_reason") or "")
    assert "response_text" not in result["outputs"], "no reply claims a handoff that did not land"
    assert inference.calls == []


# ── 3. wrong pipeline / wrong origin is refused before the gate ───────────────

def _assert_refused_without_enqueue(run, stager):
    try:
        result, inference = run()
    except ValueError:
        assert stager.calls == []
        return
    assert stager.calls == [], "refused before the queue"
    assert result["failure_kind"], "a refused handoff is a failure, not a silent reply"
    assert "response_text" not in result["outputs"]
    assert inference.calls == []


@pytest.mark.parametrize("lane", ["browser", "agent"])
@pytest.mark.parametrize("pipeline_id", [1, 2, 3])
def test_a_handoff_off_the_chat_pipeline_is_refused_without_enqueue(lane, pipeline_id,
                                                                    monkeypatch):
    stager = _Stager()

    _assert_refused_without_enqueue(
        lambda: _run(monkeypatch, lane, stager=stager, pipeline_id=pipeline_id), stager)


@pytest.mark.parametrize("lane", ["browser", "agent"])
@pytest.mark.parametrize("opened_by", ["omega_successor", "lane_work_dispatch", "autonomous", ""])
def test_a_handoff_no_operator_turn_opened_is_refused_without_enqueue(lane, opened_by,
                                                                      monkeypatch):
    stager = _Stager()

    _assert_refused_without_enqueue(
        lambda: _run(monkeypatch, lane, stager=stager, opened_by=opened_by), stager)


def test_an_operator_command_turn_may_hand_off(monkeypatch):
    stager = _Stager()

    result, _ = _run(monkeypatch, "agent", stager=stager, opened_by="operator_command")

    assert len(stager.calls) == 1 and result["failure_kind"] is None


def test_the_handoff_receipt_survives_auto_fold_for_chat_and_omega():
    receipt = {
        "status": "queued",
        "handoff_id": "h" * 64,
        "lane": "browser",
        "original_cycle_id": CYCLE,
        "thread_id": THREAD,
        "origin_ref": "operator_cycle:" + CYCLE,
    }
    pack = {"action": "RESPONSE", "execution_inputs": {"specialist_handoff": _handoff("browser")}}
    ai = AutoInput("chat-episode", {}, {}, pack, {})
    folded = _fold_episode_auto(
        ai,
        status="success",
        cycle_kind="success",
        memory_envelope=_envelope(),
        model_profile=None,
        model_id=None,
        prompt_pack=_prompt_pack(),
        exec_result={"outputs": {"response_text": "Queued for Browser.", "specialist_handoff": receipt}},
        runtime_ms=0,
    )

    outcome = auto_outcome_from_fold(ai, folded)

    assert folded["outputs"]["specialist_handoff"] == receipt
    assert outcome.auto["outputs"]["specialist_handoff"] == receipt
