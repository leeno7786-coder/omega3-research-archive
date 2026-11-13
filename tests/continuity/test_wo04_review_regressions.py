"""WO-04 independent handoff checks; use the repository's isolated continuity fixtures.

Install under tests/continuity. When run from an external scratch directory,
load tests.conftest and tests.continuity.conftest explicitly as pytest plugins.
No live model, service, canonical publication, or operator journal is used.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

import cognition_chain as chain
from global_loop.intent import context as route_context
from memory import episode_sram
from memory.episode_events import read_episode_events
from schema.continuity_json import EventIdentityConflict
from tests.continuity.test_planning_capture import (  # noqa: F401
    _bind_contract,
    _fresh_route_context,
    _global_a,
    _meta_a,
    _route_decision,
    admitted,
    router,
)


def _phase_events(episode, phase):
    return [e for e in read_episode_events(episode.episode_id) if e.phase == phase]


def _intent_events(episode):
    return [e for e in _phase_events(episode, "global_a")
            if e.event_kind in {"global_a.intent_selected", "global_a.intent_revised"}]


def _observed_value(value):
    if isinstance(value, Mapping) and value.get("state") == "observed":
        return value.get("value")
    return value


@pytest.mark.parametrize("phase", ["global_a", "meta_a"])
def test_a_raised_producer_failure_is_recorded_before_it_propagates(
    admitted, router, monkeypatch, phase
):
    """The actual sequencer/handoff runs; only its failing dependency is injected."""
    store, episode, ignition = admitted("synthetic raised-failure case")
    fault = OSError(f"review-{phase}-producer-failure")
    downstream = []
    if phase == "meta_a":
        router["decision"] = _route_decision("FREEFORM_CHAT")
        _global_a(monkeypatch, store, episode, ignition)
    before_ids = {e.event_id for e in read_episode_events(episode.episode_id)}

    def fail(*_args, **_kwargs):
        raise fault

    with monkeypatch.context() as scoped:
        if phase == "global_a":
            scoped.setattr("global_loop.loop.ignition_phase.run", fail)
            scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: downstream.append("meta"))
            invoke = lambda: chain._start_global_a(store, episode, ignition)
        else:
            scoped.setattr("meta_loop.loop.intake_phase.run", fail)
            scoped.setattr(chain, "_bind_execution_contract", lambda *_a, **_k: downstream.append("contract"))
            invoke = lambda: chain._start_meta_a(store, episode)
        with episode_sram.bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
            with pytest.raises(OSError) as caught:
                invoke()
    assert caught.value is fault, "the producer exception must survive unchanged when capture succeeds"
    assert downstream == [], "failure must not authorize the next phase"
    captured = [e for e in _phase_events(episode, phase) if e.event_id not in before_ids]
    assert captured, f"{phase} raised, but no new phase-failure event was captured"
    text = json.dumps([e.to_dict() for e in captured], sort_keys=True)
    assert str(fault) in text, "the captured failure must preserve the actual diagnostic"


def test_a_missing_intent_decision_does_not_claim_numeric_confidence(
    admitted, monkeypatch
):
    store, episode, ignition = admitted("synthetic missing decision")
    with monkeypatch.context() as scoped:
        scoped.setattr("global_loop.loop.run_global_a", lambda *_a, **_k: None)
        scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: pytest.fail("next phase ran"))
        with pytest.raises(RuntimeError, match="no IntentDecision"):
            chain._start_global_a(store, episode, ignition)
    rows = _phase_events(episode, "global_a")
    assert len(rows) == 1 and rows[0].payload["planning_status"] == "failed"
    confidence = rows[0].payload.get("intent_confidence")
    assert isinstance(confidence, Mapping), f"no decision supplied a confidence, got {confidence!r}"
    assert confidence.get("state") in {"unavailable", "not_captured", "not_applicable"}
    assert confidence.get("value") is None and confidence.get("reason")


def _make_revision(admitted, router, monkeypatch):
    router["decision"] = _route_decision("FREEFORM_CHAT", confidence=0.99, margin=0.98)
    store, episode, ignition = admitted("synthetic revision case")
    _global_a(monkeypatch, store, episode, ignition)
    original = _intent_events(episode)[-1]
    route_context.reset()
    router["decision"] = _route_decision("DOCUMENT_CREATION", confidence=0.87, margin=0.8)
    _global_a(monkeypatch, store, episode, ignition)
    return store, episode, ignition, original


def test_a_revision_retains_the_new_routes_evidence_not_only_its_label(
    admitted, router, monkeypatch
):
    _store, episode, _ignition, original = _make_revision(admitted, router, monkeypatch)
    revised = _intent_events(episode)[-1]
    assert revised.payload["supersedes_event_id"] == original.event_id
    required = {"planning_status", "route", "router_confidence", "routing_revision", "intent_confidence"}
    assert required <= set(revised.payload), f"revision lost fields: {sorted(required - set(revised.payload))}"
    assert _observed_value(revised.payload["router_confidence"]) == pytest.approx(0.87)
    assert _observed_value(revised.payload["route"])["route"] == "DOCUMENT_CREATION"
    assert _intent_events(episode)[0].sealed_sha256() == original.sealed_sha256()


def test_changed_evidence_after_revision_is_not_silently_called_an_unchanged_retry(
    admitted, router, monkeypatch
):
    store, episode, ignition, _original = _make_revision(admitted, router, monkeypatch)
    prior = _intent_events(episode)[-1]
    route_context.reset()
    router["decision"] = _route_decision("DOCUMENT_CREATION", confidence=0.76, margin=0.7)
    try:
        _global_a(monkeypatch, store, episode, ignition)
    except EventIdentityConflict:
        # A reused operation token may explicitly refuse changed immutable inputs.
        assert _intent_events(episode)[-1].sealed_sha256() == prior.sealed_sha256()
        return
    latest = _intent_events(episode)[-1]
    assert latest.event_id != prior.event_id, "changed route evidence was silently replayed as the old revision"
    assert _observed_value(latest.payload["router_confidence"]) == pytest.approx(0.76)


# Positive controls: these must stay green as the negative paths are repaired.

def test_an_identical_revision_retry_retains_its_original_occurrence(
    admitted, router, monkeypatch
):
    store, episode, ignition, _original = _make_revision(admitted, router, monkeypatch)
    before = [e.to_dict() for e in _intent_events(episode)]
    route_context.reset()
    _global_a(monkeypatch, store, episode, ignition)
    assert [e.to_dict() for e in _intent_events(episode)] == before


def test_a_real_execution_contract_has_exact_criteria_durable_at_auto_entry(
    admitted, monkeypatch
):
    from meta_loop import execution_contract

    store, episode, _ignition = admitted("synthetic contracted check")
    objective = "Collect the declared synthetic source evidence"
    criteria = ["The requested source is identified", "The returned evidence is checked"]
    episode.meta["execution_action_pack"] = {
        "episode_id": episode.episode_id,
        "action": "EXECUTE_MOTOR_PROGRAM",
        "tool": "web_fetch",
        "execution_inputs": {
            "motor_transport": "USE_TOOL",
            "query": objective,
            "emergent_pressure": {
                "resolution_conditions": criteria,
                "proposed_action": {"required_capabilities": ["web_research"]},
            },
        },
    }
    reached = []

    def at_auto():
        rows = [e for e in read_episode_events(episode.episode_id)
                if e.event_kind == "meta_a.acceptance_criteria"]
        assert len(rows) == 1
        payload = rows[0].payload
        assert payload["status"] == "contracted"
        assert _observed_value(payload["objective"]) == objective
        assert list(_observed_value(payload["criteria"])) == criteria
        assert _observed_value(payload["criteria_revision"]) == 1
        reached.append(rows[0].event_id)

    with monkeypatch.context() as scoped:
        scoped.setattr(execution_contract, "motor_programs_enabled", lambda: True)
        scoped.setattr(execution_contract, "motor_programs_eligible_actions", lambda: {"EXECUTE_MOTOR_PROGRAM"})
        _bind_contract(monkeypatch, store, episode, at_auto=at_auto)
    assert len(reached) == 1
