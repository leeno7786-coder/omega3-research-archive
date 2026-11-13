from types import SimpleNamespace

import pytest

import cognition_chain as chain
import pressure_loop.proactive_chat_messages as proactive_chat
import pressure_loop.proactive_lifecycle as lifecycle
from global_loop.intent.run import run as run_global_a
from meta_loop.action.run import run as run_meta_action
from meta_loop.intake.run import MetaAInput
from omega_loop.episode.prompts import prompt_spec_for
from omega_loop.episode.types import MasteryView, OmegaEpisodeInput
from pressure_loop.contributors import EpisodeIgnition
from pressure_loop.pressure import _proactive_lifecycle_contributor


SCENARIOS = (
    ("Number of heads in 20 coin flips", "Binomial", "fixed independent trials"),
    ("Emails arriving in one hour", "Poisson", "event count over an interval"),
    ("Adult heights in a population", "Normal", "continuous many-factor measurement"),
    ("Defective bulbs in a fixed sample of 50", "Binomial", "fixed success-failure trials"),
    ("Calls reaching a help desk in ten minutes", "Poisson", "event count over time"),
    ("Measurement error from many small influences", "Normal", "many small continuous effects"),
    ("Patients responding among 40 treated patients", "Binomial", "fixed binary trials"),
    ("Meteor sightings per night", "Poisson", "events counted in an interval"),
    ("Manufactured bolt diameters", "Normal", "continuous values around a mean"),
    ("Sixes observed in 30 die rolls", "Binomial", "fixed independent success-failure trials"),
)


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    import pressure_loop.system_messages as system_messages

    monkeypatch.setattr(lifecycle, "_ledger_path", lambda: tmp_path / "proactive_lifecycle.jsonl")
    monkeypatch.setattr(proactive_chat, "_store_path", lambda: tmp_path / "proactive_chat.json")
    monkeypatch.setattr(system_messages, "_store_path", lambda: tmp_path / "system_messages.json")
    monkeypatch.setattr(system_messages, "_attach_journal_answer", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input", lambda *_a, **_k: None
    )
    lifecycle._reset_cache_for_tests()
    proactive_chat._reset_cache_for_tests()
    system_messages._reset_cache_for_tests()
    yield
    lifecycle._reset_cache_for_tests()
    proactive_chat._reset_cache_for_tests()
    system_messages._reset_cache_for_tests()


def _seed_applying_policy():
    thread = lifecycle.record_ask(
        prompt_id="prompt-probability",
        episode_id="ep-ask",
        question="Continue foundational clarification or pivot?",
        why="Scenario classification is not yet proven.",
        pressure_winner="clarification_gap",
        expected_response="A bounded curriculum policy.",
        options=(),
    )
    policy = lifecycle.bind_operator_policy(
        prompt_id="prompt-probability",
        answer_episode_id="ep-answer",
        operator_text=(
            "Continue with one more scenario-based pass. Distinguish Binomial, "
            "Poisson, and Normal, then classify 10 scenarios and explain each choice. "
            "Do not pivot entirely unless classification remains weak."
        ),
    )
    policy = lifecycle.structure_operator_policy(
        policy_id=policy.policy_id,
        objective="Distinguish and apply Binomial, Poisson, and Normal distributions.",
        constraints=("Do not pivot entirely unless scenario classification remains weak.",),
        learning_steps=("Use simple scenarios for one foundational pass.",),
        requested_demonstrations=("Classify 10 scenarios and explain each choice.",),
        continuation_rule="Continue Binomial-vs-Poisson boundary practice when weak.",
    )
    lifecycle.mark_learning(thread.thread_id, episode_id="ep-learn")
    lifecycle.mark_applying(thread.thread_id, episode_id="ep-learn")
    return thread, policy


def _dispatch_pack():
    contributor = _proactive_lifecycle_contributor()
    assert contributor is not None
    ignition = EpisodeIgnition(
        episode_id="ep-present",
        aggregate_pressure=1.0,
        opened_by="proactive_update",
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )
    assert run_global_a(ignition) == "PROACTIVE_RESPONSE"
    state = run_meta_action(
        MetaAInput(
            episode_id="ep-present",
            pressure={
                "reason": "proactive_update",
                "contributors": [
                    {
                        "id": contributor.type,
                        "weight": contributor.weight,
                        "detail": dict(contributor.metadata),
                    }
                ],
                "aggregate_pressure": 1.0,
            },
            trajectory_snapshot={},
            action_goal={
                "episode_id": "ep-present",
                "intent": "PROACTIVE_RESPONSE",
                "confidence": 1.0,
                "reason": "present curriculum application",
            },
        )
    )
    assert state.action == "PROACTIVE_RESPONSE"
    return contributor, state


def _omega_packet(state, policy):
    dispatch = {
        "presentation_kind": "PRESENT_RESULT",
        "proactive_thread_id": state.execution_inputs["proactive_thread_id"],
        "proactive_policy_id": state.execution_inputs["proactive_policy_id"],
        "proactive_application_id": state.execution_inputs["proactive_application_id"],
        "operator_policy": policy.to_dict(),
    }
    return OmegaEpisodeInput(
        episode_id="ep-present",
        intent="PROACTIVE_RESPONSE",
        action="PROACTIVE_RESPONSE",
        tool=None,
        domain="probability distributions",
        operator_text=None,
        outcome="success",
        failure_reason=None,
        raw_tool_result=None,
        recall=({"content": "fixed trials; interval counts; continuous measurements"},),
        recall_error=None,
        prior_mastery=MasteryView(
            domain="probability distributions",
            knowledge_mastery=0.75,
            application_mastery=0.45,
            target_mastery=0.7,
        ),
        response_text="\n".join(
            f"{index}. {scenario}: {choice}, because {why}."
            for index, (scenario, choice, why) in enumerate(SCENARIOS, start=1)
        ),
        proactive_policy=policy.to_dict(),
        proactive_dispatch=dispatch,
    )


def _control(*, weak=False):
    """Omega's strict control envelope: weak work owes LEARN pressure, passed work is DONE."""
    assessment = {
        **dict.fromkeys(
            ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
            "the ten-scenario application result",
        ),
        "gap": "Binomial vs Poisson boundary cases remain weak" if weak else "",
        "evidence": ["executor:ep-present"],
    }
    if not weak:
        return {"assessment": assessment, "decision": "DONE", "pressure": None,
                "synthesis": "Policy satisfied."}
    return {
        "assessment": assessment,
        "decision": "PRESSURE",
        "pressure": {
            "intent": "LEARN",
            "domain": "probability distributions",
            "topic": "Binomial vs Poisson boundary cases",
            "confidence": 0.9,
            "reason": "Correct the weak boundary",
            "setpoints": {"mastery_debt_delta": 0.9},
            "desired_state": "boundary scenarios classified correctly",
            "observed_state": "scenarios 2 and 8 misclassified",
            "resolution_conditions": ["boundary scenarios verified correct"],
        },
        "synthesis": "Correct the weak boundary.",
    }


def _payload(*, weak=False):
    rows = [
        {
            "prompt": scenario,
            "answer": choice,
            "expected": choice,
            "correct": True,
            "explanation": f"This uses {why}.",
            "evidence": [f"executor:row-{index}"],
        }
        for index, (scenario, choice, why) in enumerate(SCENARIOS, start=1)
    ]
    if weak:
        rows[1].update(
            answer="Binomial",
            correct=False,
            explanation="I treated an interval count as fixed trials.",
        )
        rows[7].update(
            answer="Binomial",
            correct=False,
            explanation="I did not check whether the trial count was fixed.",
        )
    return {
        "control": _control(weak=weak),
        "response_text": "I completed the ten-scenario distribution pass.",
        "proactive_result": {
            "title": "Binomial, Poisson, and Normal scenario classification",
            "summary": "Completed all ten requested classifications.",
            "practiced": ["Binomial", "Poisson", "Normal"],
            "learned": ["Check fixed trials before interval event counts."],
            "application": rows,
            "evidence_refs": ["executor:ep-present"],
            "confidence": 0.58 if weak else 0.92,
            "mastery_status": "weak" if weak else "passed",
            "mastery_evidence": ["8/10 verified" if weak else "10/10 verified"],
            "mistakes": ["Scenarios 2 and 8"] if weak else [],
            "weak_patterns": (
                ["fixed trial count was not checked before selecting Binomial"] if weak else []
            ),
            "next_step": (
                "Practice Binomial vs Poisson boundary cases."
                if weak
                else "Move into estimation and hypothesis testing."
            ),
            "operator_input_needed": False,
        },
        "mastery": {
            "knowledge_mastery": 0.75,
            "application_mastery": 0.58 if weak else 0.76,
            "rationale": "Scenario answers were compared row by row.",
        },
        "pressure_params": {"mastery_debt_delta": 0.9} if weak else {},
        "intent_decision": {
            "intent": "LEARN" if weak else "IDLE",
            "domain": "probability distributions",
            "topic": "Binomial vs Poisson boundary cases" if weak else "",
            "confidence": 0.9,
            "reason": "Correct the weak boundary" if weak else "Policy satisfied",
        },
    }


def _close_result(packet, result, contributor):
    episode = SimpleNamespace(
        episode_id=packet.episode_id,
        opened_by="proactive_update",
        global_={
            "intent_decision": {"intent": "PROACTIVE_RESPONSE"},
            "omega_packet": packet.to_dict(),
            "outcome": {"artifacts": {"response_text": result.response_text}},
        },
        meta={"execution_action_pack": {"action": "PROACTIVE_RESPONSE"}},
        auto={"status": "completed", "outputs": {}},
        pressure={"contributors": [{"id": contributor.type, "detail": dict(contributor.metadata)}]},
    )
    episode.global_["omega_result"] = result.to_dict()
    chain._advance_proactive_lifecycle(episode, result)
    chain._record_proactive_chat(episode)
    return episode


@pytest.mark.parametrize("weak", (False, True))
def test_probability_policy_presents_to_panel_and_folds_on_grade(stores, weak):
    """Operator 2026-07-12: the result posts like a question — pending in the
    panel until the operator responds — and only the GRADE folds the thread."""
    import pressure_loop.system_messages as system_messages

    thread, policy = _seed_applying_policy()
    contributor, state = _dispatch_pack()
    packet = _omega_packet(state, policy)
    result = prompt_spec_for(packet).parse(_payload(weak=weak), packet)
    _close_result(packet, result, contributor)

    # Presentation: a pending QUESTIONS-PANEL record, badge-counted, no feed post.
    panel = [
        rec for rec in system_messages.list_messages() if rec.get("kind") == "result_presentation"
    ]
    assert len(panel) == 1
    presented = panel[0]
    assert presented["status"] == system_messages.STATUS_PENDING
    assert "Why:" in presented["text"]
    assert system_messages.unread_count() == 1
    assert proactive_chat.list_pending() == []
    # No fold before the grade — the mind does not grade its own homework.
    assert lifecycle.foldback_for_thread(thread.thread_id) is None
    assert lifecycle.thread_state(thread.thread_id) == "result_presented"
    if weak:
        assert result.mastery_effects == ()
        assert "Mastery status: Not yet passed" in presented["text"]
    else:
        assert result.mastery_effects

    # The operator grades it → fold fires with the stashed delta.
    graded = system_messages.respond_to_message(
        presented["id"], "Graded — keep going." if weak else "Graded — passed."
    )
    assert graded is not None and graded["status"] == system_messages.STATUS_RESPONDED
    foldback = lifecycle.foldback_for_thread(thread.thread_id)
    assert foldback is not None
    if weak:
        assert foldback.mastery_delta == 0.0
        assert foldback.resolution == "continue"
        assert "Binomial" in foldback.next_pressure_candidates[0]
        assert "Poisson" in foldback.next_pressure_candidates[0]
        assert lifecycle.thread_state(thread.thread_id) == "continuing"
    else:
        assert foldback.mastery_delta > 0.0
        assert foldback.resolution == "resolve"
        assert lifecycle.thread_state(thread.thread_id) == "resolved"


def test_restart_does_not_duplicate_presentation_and_grade_folds_once(stores):
    import pressure_loop.system_messages as system_messages

    thread, policy = _seed_applying_policy()
    contributor, state = _dispatch_pack()
    packet = _omega_packet(state, policy)
    result = prompt_spec_for(packet).parse(_payload(weak=True), packet)
    episode = _close_result(packet, result, contributor)

    lifecycle._reset_cache_for_tests()
    system_messages._reset_cache_for_tests()
    chain._record_proactive_chat(episode)
    lifecycle._reset_cache_for_tests()

    panel = [
        rec for rec in system_messages.list_messages() if rec.get("kind") == "result_presentation"
    ]
    assert len(panel) == 1, "a restart must not re-present the same result"
    path = lifecycle._ledger_path()
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert sum('"event_type": "result_presented"' in line for line in rows) == 1
    assert sum('"event_type": "folded"' in line for line in rows) == 0

    # Grade after the restart — exactly one fold.
    system_messages.respond_to_message(panel[0]["id"], "Graded.")
    system_messages.respond_to_message(panel[0]["id"], "Graded again.")  # idempotent
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert sum('"event_type": "folded"' in line for line in rows) == 1
    assert lifecycle.foldback_for_thread(thread.thread_id) is not None
