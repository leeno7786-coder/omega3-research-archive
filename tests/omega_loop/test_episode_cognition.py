"""Slice 2A — per-episode Omega cognition (run_omega_for_episode), RED-first.

Pins the contract + every fail-mode of the per-episode cognitive pass:
  - every packet, including an IDLE observation, folds once through its lane
  - LEARN (agent lane, acquisition evidence): grounded knowledge_to_ingest +
    ABSOLUTE mastery_effects; the next step comes only from the control envelope
  - RESPONSE (chat lane): inward reflection, the reply is never re-emitted
  - CONTAINED (a thought that failed — the organism lives, the episode closes
    failed): a lane missing from the prompt table, no model output after bounded
    retry, unparseable or contract-violating output
  - FATAL (substrate — nothing trustworthy to reason over): a packet carrying
    recall_error, a missing Global B packet. These must NOT be catchable as
    fold failures.
  - zero runtime leakage: injected reason_fn means invoke_runtime_generate never runs
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from omega_loop.episode.run import EpisodeCognitionParseError, run_omega_for_episode


def _episode_with_packet(**overrides):
    packet = {
        "episode_id": "ep-cog-1",
        "intent": "LEARN",
        "action": "USE_TOOL",
        "omega_lane": "agent",
        "tool": "web_fetch",
        "domain": "quantum_physics",
        "operator_text": None,
        "outcome": "success",
        "failure_reason": None,
        "raw_tool_result": {"results": [{"title": "Qubits", "snippet": "superposition"}]},
        "recall": [],
        "recall_error": None,
        "prior_mastery": {
            "domain": "quantum_physics",
            "knowledge_mastery": 0.2,
            "application_mastery": 0.1,
            "target_mastery": 0.7,
        },
    }
    packet.update(overrides)
    return SimpleNamespace(episode_id=packet["episode_id"], global_={"omega_packet": packet})


def _control(**pressure):
    """The strict Omega envelope: DONE by default, PRESSURE when pressure fields are given."""
    assessment = dict.fromkeys(
        ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
        "episode evidence",
    )
    if not pressure:
        return {
            "assessment": {**assessment, "gap": "", "evidence": ["tool result"]},
            "decision": "DONE",
            "pressure": None,
            "synthesis": "grounded summary",
        }
    return {
        "assessment": {**assessment, "gap": "the next sub-aspect is unknown", "evidence": ["tool result"]},
        "decision": "PRESSURE",
        "pressure": {
            "confidence": 0.8,
            "reason": "the next sub-aspect is unknown",
            "setpoints": {"mastery_debt_delta": 0.9},
            "desired_state": "sub-aspect covered",
            "observed_state": "basics covered",
            "resolution_conditions": ["sub-aspect grounded"],
            **pressure,
        },
        "synthesis": "grounded summary",
    }


def _never(*_a, **_k):
    raise AssertionError("model must not be invoked")


# --- B2: APPLY execution-evidence survives packet reconstruction ---------


def test_packet_from_episode_preserves_applied_evidence():
    """The serialized omega_packet carries `applied` (APPLY execution evidence);
    the production reconstruction must read it back, not silently default to None
    — which would ground application_mastery blind and defeat the non-executed-APPLY
    guard. Drives the PRODUCTION reconstruction (_packet_from_episode), not OEI(**raw)."""
    from omega_loop.episode.run import _packet_from_episode

    applied = {
        "status": "applied",
        "summary": "edited auto_loop/execute/run.py",
        "has_diff": True,
        "files_touched": 2,
        "error": None,
    }
    ep = _episode_with_packet(intent="APPLY", action="APPLY", tool=None, applied=applied)
    oei = _packet_from_episode(ep)
    assert oei is not None
    assert oei.applied == applied


# --- gate ----------------------------------------------------------------


def test_idle_episode_folds_through_its_lane_like_every_episode():
    """Omega runs once per ordinary episode (realignment spec): an IDLE observation is
    folded like any other packet and earns nothing it did not evidence."""
    ep = _episode_with_packet(intent="IDLE", action="IDLE", omega_lane="chat", tool=None,
                              domain=None, raw_tool_result=None, prior_mastery=None)
    res = run_omega_for_episode(ep, reason_fn=lambda s, u: json.dumps({"control": _control()}))
    assert res.cognized is True
    assert res.knowledge_to_ingest == ()
    assert res.mastery_effects == ()
    assert res.pressure_params == {}
    assert res.response_text is None
    assert res.control_decision["decision"] == "DONE"



def test_prompt_table_gap_folds_a_contained_failure_not_a_crash(monkeypatch):
    """A prompt-table gap is OUR omission, never the mind's corruption — the
    same judgement as IMAGE_CREATION (2026-08-10). Lanes own dispatch now, so the
    gap is a lane missing from the table; it must cost ONE episode's fold, not
    the organism, and never reach the model. Pinned as a type, not a message."""
    from omega_loop.episode import prompts
    from omega_loop.episode.run import (
        EpisodeCognitionFoldError,
        EpisodeCognitionUnmappedActionError,
    )

    monkeypatch.delitem(prompts.LANE_SPECS, "agent")
    with pytest.raises(EpisodeCognitionUnmappedActionError, match="no cognition prompt"):
        run_omega_for_episode(_episode_with_packet(), reason_fn=_never)
    assert issubclass(EpisodeCognitionUnmappedActionError, EpisodeCognitionFoldError)


def test_unfamiliar_action_is_evidence_inside_its_lane():
    """An action name the table has never seen is not a gap: the lane owns the prompt."""
    ep = _episode_with_packet(intent="APPLY", action="TELEPATHY", tool=None)
    res = run_omega_for_episode(ep, reason_fn=lambda s, u: json.dumps({"control": _control()}))
    assert res.cognized is True


def test_missing_global_b_packet_fails_instead_of_skipping_omega():
    episode = SimpleNamespace(episode_id="missing-packet", global_={})
    with pytest.raises(RuntimeError, match="missing Global B omega packet"):
        run_omega_for_episode(episode, reason_fn=_never)


# --- LEARN grounded cognition -------------------------------------------


def test_learn_produces_grounded_knowledge_and_absolute_mastery():
    payload = {
        "control": _control(),
        "knowledge_to_ingest": [
            {
                "domain": "quantum_physics",
                "statement": "A qubit holds superposition.",
                "confidence": 0.9,
            }
        ],
        "mastery": {
            "knowledge_mastery": 0.45,
            "application_mastery": 0.1,
            "rationale": "covered superposition basics, not yet entanglement",
        },
        # Legacy authority: ignored, because pressure travels only in control.
        "pressure_params": {"application_debt_delta": 0.1},
    }
    captured = {}

    def fake(system, user):
        captured["system"], captured["user"] = system, user
        return json.dumps(payload)

    res = run_omega_for_episode(
        _episode_with_packet(
            neuromod_state={
                "dopamine": -0.2,
                "serotonin": 0.15,
                "stress": 0.4,
                "spike": 0.0,
            }
        ),
        reason_fn=fake,
    )
    assert res.cognized is True
    assert len(res.knowledge_to_ingest) == 1
    assert res.knowledge_to_ingest[0].statement == "A qubit holds superposition."
    # ABSOLUTE grounded estimate (not an increment off prior 0.2)
    assert len(res.mastery_effects) == 1
    assert res.mastery_effects[0].knowledge_mastery == 0.45
    assert res.mastery_effects[0].domain == "quantum_physics"
    assert res.response_text is None
    assert res.pressure_params == {}
    # the packet's raw result + prior mastery must reach the prompt
    assert "superposition" in captured["user"]
    assert "knowledge_to_ingest" in captured["system"]
    assert "Neuromodulation is advisory" in captured["system"]
    assert "Low serotonin reduces assertiveness" in captured["system"]
    assert "never replace or suppress" in captured["system"]


def test_onboarding_learn_foldback_plans_bounded_child_topics():
    context = {
        "occupation": "software_engineer",
        "categories": [
            {"title": "Python", "lobe": 8, "y": 0},
            {"title": "System Design", "lobe": 2, "y": 1},
        ],
        "source_citation": {"source_id": "src-onboarding"},
    }
    payload = {
        "control": _control(),
        "knowledge_to_ingest": [],
        "mastery": {
            "knowledge_mastery": 0.3,
            "application_mastery": 0.1,
            "rationale": "initial topic pass",
        },
        "onboarding_subtopics": [
            {"parent": "Python", "subtopics": ["asyncio", "Decorators", "asyncio"]},
            {"parent": "Not Onboarded", "subtopics": ["must be rejected"]},
            {"parent": "System Design", "subtopics": ["Load balancing"]},
        ],
    }
    captured = {}

    def fake(system, user):
        captured["system"] = system
        captured["user"] = user
        return json.dumps(payload)

    result = run_omega_for_episode(
        _episode_with_packet(onboarding_context=context),
        reason_fn=fake,
    )

    assert "onboarding_subtopics" in captured["system"]
    assert "software_engineer" in captured["user"]
    assert result.onboarding_subtopics == (
        {"parent": "Python", "subtopics": ["asyncio", "Decorators"]},
        {"parent": "System Design", "subtopics": ["Load balancing"]},
    )


def test_learn_next_step_comes_only_from_the_control_envelope():
    """The retired model-authored intent_decision can no longer prepare work; the
    validated control pressure is projected once into the legacy field."""
    base = {
        "knowledge_to_ingest": [],
        "mastery": {"knowledge_mastery": 0.4, "application_mastery": 0.1, "rationale": "r"},
    }
    legacy = {
        **base,
        "control": _control(),
        "intent_decision": {
            "intent": "APPLY",
            "domain": "quantum_physics",
            "confidence": 0.8,
            "reason": "ready to practice",
        },
    }
    res = run_omega_for_episode(_episode_with_packet(), reason_fn=lambda s, u: json.dumps(legacy))
    assert res.intent_decision is None

    steered = {**base, "control": _control(intent="LEARN", domain="quantum_physics", topic="entanglement")}
    res = run_omega_for_episode(_episode_with_packet(), reason_fn=lambda s, u: json.dumps(steered))
    assert res.intent_decision == {
        "intent": "LEARN",
        "confidence": 0.8,
        "reason": "the next sub-aspect is unknown",
        "domain": "quantum_physics",
        "topic": "entanglement",
    }
    assert res.pressure_params == {"mastery_debt_delta": 0.9}


def test_learn_that_fetched_nothing_new_does_not_invent_mastery_gain():
    # empty recall + empty result => grounded estimate may equal prior, not +inc
    payload = {
        "control": _control(),
        "knowledge_to_ingest": [],
        "mastery": {
            "knowledge_mastery": 0.2,
            "application_mastery": 0.1,
            "rationale": "nothing new",
        },
    }
    res = run_omega_for_episode(
        _episode_with_packet(raw_tool_result={"results": []}, recall=[]),
        reason_fn=lambda s, u: json.dumps(payload),
    )
    assert res.mastery_effects[0].knowledge_mastery == 0.2  # no fabricated climb


# --- RESPONSE ------------------------------------------------------------


def test_response_action_reflects_without_reemitting_the_reply():
    ep = _episode_with_packet(
        intent="RESPOND",
        action="RESPONSE",
        omega_lane="chat",
        tool=None,
        domain=None,
        operator_text="What is a qubit?",
        raw_tool_result=None,
        prior_mastery=None,
    )
    reflection = (
        "I led with a concrete definition, then used it to make the unfamiliar term approachable.\n\n"
        "Trait candidates:\n"
        "- define_concretely — I lead with a concrete definition when explaining a new concept "
        "| evidence: the completed qubit reply "
        "| status: new"
    )
    res = run_omega_for_episode(ep, reason_fn=lambda s, u: reflection)
    assert res.cognized is True
    assert res.response_text is None
    assert res.reflection == reflection
    assert res.identity_patterns[0]["trait"] == "define_concretely"
    assert res.mastery_effects == ()


# --- bounded fold failures (contained) vs. substrate (fatal) -------------


def test_persistent_no_output_raises_the_contained_fold_type():
    """An EMPTY model response is a transport condition — the worker was
    unavailable or the preflight declined — not substrate poison: no false
    thought entered the mind, one thought just did not happen. It must raise the
    CONTAINED type so the cascade folds an honest failure and reaches its
    terminus, instead of the bare RuntimeError that killed the brain
    (2026-08-11)."""
    from omega_loop.episode.run import (
        EpisodeCognitionFoldError,
        EpisodeCognitionNoOutputError,
    )

    with pytest.raises(EpisodeCognitionNoOutputError, match="no output"):
        run_omega_for_episode(_episode_with_packet(), reason_fn=lambda s, u: None)
    assert issubclass(EpisodeCognitionNoOutputError, EpisodeCognitionFoldError)


def test_persistent_no_output_stays_loud_in_telemetry():
    """Containment must not become a silencer. Before this, the crash WAS the
    record of a silent model and no attempt reached the output emit (which needs
    text). Every empty attempt now emits its own event, so a declining worker is
    still visible after the organism stops dying of it."""
    import omega_loop.episode.run as run_mod

    events: list[dict] = []

    def _emit(surface, event, **fields):
        events.append({"surface": surface, "event": event, **fields})

    monkey = pytest.MonkeyPatch()
    # Patch at the guardian module the emitter imports from (function-local import).
    import guardian.terminal_feedback as tf

    monkey.setattr(tf, "emit_data", _emit)
    try:
        with pytest.raises(run_mod.EpisodeCognitionNoOutputError):
            run_omega_for_episode(_episode_with_packet(), reason_fn=lambda s, u: None)
    finally:
        monkey.undo()

    silent = [e for e in events if e["event"] == "episode_model_no_output"]
    assert len(silent) == 2, "every bounded attempt must report its silence"
    assert all(e["ok"] is False and e["status"] == "degraded" for e in silent)
    assert silent[0]["attempt"] == 1 and silent[1]["attempt"] == 2


def test_transient_no_output_retries_then_succeeds():
    """A transient inference miss (a runtime hiccup under load — the live 7.7s
    blip) is retried within the bounded loop instead of failing the episode:
    None once, then valid JSON -> cognition succeeds (no raise, no lost
    grounding). Only a PERSISTENT no-output fails hard."""
    calls = {"n": 0}
    payload = {
        "control": _control(),
        "knowledge_to_ingest": [],
        "mastery": {
            "knowledge_mastery": 0.3,
            "application_mastery": 0.1,
            "rationale": "recovered",
        },
    }

    def flaky(_system, _user):
        calls["n"] += 1
        return None if calls["n"] == 1 else json.dumps(payload)

    res = run_omega_for_episode(_episode_with_packet(), reason_fn=flaky)
    assert calls["n"] == 2  # retried once after the transient miss
    assert res.cognized is True
    assert res.mastery_effects[0].knowledge_mastery == 0.3


def test_unparseable_output_fails_hard_after_retry():
    calls = {"n": 0}

    def garbage(system, user):
        calls["n"] += 1
        return "the model rambled with no json"

    with pytest.raises(EpisodeCognitionParseError):
        run_omega_for_episode(_episode_with_packet(), reason_fn=garbage)
    assert calls["n"] >= 2  # bounded retry attempted before fail-hard


def test_recall_error_packet_fails_hard_no_cognition():
    ep = _episode_with_packet(recall_error="read_memory timeout", outcome="failure")
    with pytest.raises(RuntimeError):
        run_omega_for_episode(ep, reason_fn=_never)


def test_substrate_failures_are_not_contained_fold_failures():
    """THE LINE. Softening the bounded failures must never soften the substrate
    ones: a poisoned recall and a missing Global B packet leave nothing
    trustworthy to reason over, so they stay FATAL and must not be catchable by
    the containment handler. Asserted as `not isinstance` because that handler
    catches exactly EpisodeCognitionFoldError — if either of these ever became
    one, the mind would quietly cognize on poison."""
    from omega_loop.episode.run import EpisodeCognitionFoldError

    poisoned = _episode_with_packet(recall_error="read_memory timeout", outcome="failure")
    with pytest.raises(RuntimeError) as poison_exc:
        run_omega_for_episode(poisoned, reason_fn=_never)
    assert not isinstance(poison_exc.value, EpisodeCognitionFoldError)

    with pytest.raises(RuntimeError) as missing_exc:
        run_omega_for_episode(
            SimpleNamespace(episode_id="missing-packet", global_={}), reason_fn=_never
        )
    assert not isinstance(missing_exc.value, EpisodeCognitionFoldError)
