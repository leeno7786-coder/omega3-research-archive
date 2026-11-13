"""Agent and browser turns answer the operator when Omega settles the work (plan Task 12).

Live 2026-09-14: "open the browser to omega2ai.com" ran two browser episodes, Omega
said DONE, and the operator's bubble spun forever. Agent and browser AUTO passes
publish progress but never answer; the chain's settling Omega decision does.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cognition_chain as cl
import pressure_loop.operator_chat_responses as ocr
from pressure_loop import pressure
from schema.omega_control_decision import OmegaControlDecision


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_store_path", lambda: tmp_path / "ocr.json")
    ocr._reset_cache_for_tests()
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})


def control(decision="PRESSURE", intent="USE_SKILL", synthesis="summary"):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "fact")
    assessment.update(gap="" if decision == "DONE" else "unfinished", evidence=[])
    pressure_decision = None if decision == "DONE" else dict(
        intent=intent, confidence=0.9, reason="finish", setpoints={}, desired_state="complete",
        observed_state="partial", resolution_conditions=["verified"])
    return OmegaControlDecision.from_dict(dict(assessment=assessment, decision=decision,
                                               pressure=pressure_decision, synthesis=synthesis))


def operator_episode(lane="agent", response_text=""):
    return SimpleNamespace(
        episode_id="ep-operator", pipeline_id=0, work_kind="operator_turn", opened_by="operator_query",
        global_={"intent_decision": {"intent": "RESPOND", "omega_lane": lane},
                 "omega_packet": {"original_objective": "tidy the repo"}},
        meta={"execution_action_pack": {"action": "RESPONSE"}},
        auto={"status": "completed", "outputs": {"response_text": response_text}},
        pressure={"contributors": [{"id": "operator_query", "detail": {
            "cycle_id": "cyc-1", "chat_bubble_waiting": True, "message": "tidy the repo"}}]})


def successor_episode(row):
    return SimpleNamespace(
        episode_id="ep-successor", pipeline_id=0, work_kind="autonomous", opened_by="omega_successor",
        global_={"intent_decision": {"intent": "USE_SKILL", "omega_lane": "agent"}},
        meta={}, auto={"status": "completed", "outputs": {}},
        pressure={"contributors": [{"id": "omega_successor", "detail": dict(row.metadata)}]})


@pytest.mark.parametrize("auto", [
    {"status": "completed", "outputs": {"response_text": "halfway there"}},
    {"status": "failed", "failure_kind": "browser_inference_failed", "failure_reason": "x", "outputs": {}},
])
def test_agent_and_browser_turns_do_not_answer_at_auto(auto):
    episode = operator_episode()
    episode.auto = auto

    cl._deliver_chat_reply_at_auto(episode)
    cl._record_operator_outcome(MagicMock(), episode)

    assert ocr.get_response("cyc-1") is None


def test_the_bubble_waits_through_successors_until_omega_is_done():
    first = operator_episode()
    cl._settle_operator_turn(first, control("PRESSURE"))
    assert ocr.get_response("cyc-1") is None

    (row,) = pressure.staged_signals()
    successor = successor_episode(row)
    # The successor's tool frames and answer address the operator's original bubble.
    assert ocr.waiting_chat_turn(successor.pressure) == {"cycle_id": "cyc-1", "chat_bubble_waiting": True}

    cl._settle_operator_turn(successor, control("DONE", synthesis="The repo is tidy: three files moved."))

    assert ocr.get_response("cyc-1")["text"] == "The repo is tidy: three files moved."
    assert successor.meta["chat_reply_delivered_cycle"] == "cyc-1"


def test_done_prefers_the_work_s_own_reply_over_the_synthesis():
    episode = operator_episode(response_text="Here is the directory listing.")

    cl._settle_operator_turn(episode, control("DONE", synthesis="I listed the directory."))

    assert ocr.get_response("cyc-1")["text"] == "Here is the directory listing."


def test_ask_operator_puts_the_question_in_the_waiting_bubble():
    episode = operator_episode(lane="browser")

    cl._settle_operator_turn(episode, control("ASK_OPERATOR", intent="ASK_OPERATOR",
                                              synthesis="Which account should I sign in with?"))

    assert ocr.get_response("cyc-1")["text"] == "Which account should I sign in with?"
    assert pressure.staged_signals() == ()


def test_a_continuation_that_cannot_be_staged_still_answers(monkeypatch):
    monkeypatch.setattr("pressure_loop.omega_successor.build_omega_successor", lambda *_: None)

    cl._settle_operator_turn(operator_episode(), control(synthesis="The workflow closed before I could finish."))

    assert "closed before" in ocr.get_response("cyc-1")["text"]


def test_a_failed_fold_answers_with_the_failure():
    episode = operator_episode()
    episode.auto.update(failure_kind="browser_action_failed", failure_reason="click target missing")

    cl._settle_operator_turn(episode, None)

    record = ocr.get_response("cyc-1")
    assert record["text"] == "I could not complete that: click target missing"
    assert record["delivery"]["failed"] is True


def test_browser_done_leaves_the_workflow_waiting_for_the_operator(monkeypatch):
    waiting = []
    monkeypatch.setattr("meta_loop.browser.workflow.mark_browser_waiting", waiting.append)
    episode = operator_episode(lane="browser")
    episode.global_["browser_workflow"] = {"workflow_id": "wf-1"}

    cl._settle_operator_turn(episode, control("DONE", synthesis="omega2ai.com is open."))

    assert waiting == ["wf-1"]


def test_chat_answers_at_auto_and_its_successors_do_not_reopen_the_bubble():
    episode = operator_episode(lane="chat", response_text="Hi there.")

    cl._deliver_chat_reply_at_auto(episode)
    cl._settle_operator_turn(episode, control("PRESSURE"))

    assert ocr.get_response("cyc-1")["text"] == "Hi there."
    (row,) = pressure.staged_signals()
    assert "chat_bubble_waiting" not in row.metadata
