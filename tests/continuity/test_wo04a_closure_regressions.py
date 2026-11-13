"""WO-04A closure tests: isolated lifecycle failures and complete decision evidence.

Install under tests/continuity. External scratch execution must load tests.conftest
and tests.continuity.conftest as plugins. No model or canonical publication runs.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

import cognition_chain as chain
from memory import cognition_capture as capture
from memory.episode_events import read_episode_events
from schema.continuity_json import ContinuityContractError, EventIdentityConflict
from tests.continuity import test_planning_capture as planning
from tests.continuity import test_wo04_review_regressions as earlier

# Reuse the real admission fixtures without fixture-import/parameter redefinitions.
admitted = planning.admitted
router = planning.router
_fresh_route_context = planning._fresh_route_context


def _events(episode):
    return list(read_episode_events(episode.episode_id))


def _new_events(episode, before):
    return [event for event in _events(episode) if event.event_id not in before]


def _failure_text(events):
    return json.dumps([event.to_dict() for event in events], sort_keys=True)


def test_failed_reselection_records_failure_without_superseding_valid_decision(
    admitted, router, monkeypatch
):
    router["decision"] = planning._route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("synthetic established decision")
    planning._global_a(monkeypatch, store, episode, ignition)
    valid = earlier._intent_events(episode)[-1]
    before = {event.event_id for event in _events(episode)}
    fault = OSError("closure: failed to make a replacement decision")
    downstream = []

    def fail(*_args, **_kwargs):
        raise fault

    with monkeypatch.context() as scoped:
        scoped.setattr("global_loop.loop.ignition_phase.run", fail)
        scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: downstream.append("meta"))
        with pytest.raises(OSError) as caught:
            chain._start_global_a(store, episode, ignition)
    assert caught.value is fault
    assert downstream == []
    fresh = _new_events(episode, before)
    assert fresh and str(fault) in _failure_text(fresh)
    assert next(event for event in _events(episode) if event.event_id == valid.event_id).sealed_sha256() == valid.sealed_sha256()
    invalidating = [event for event in fresh
                    if event.event_kind == "global_a.intent_revised"
                    and event.payload.get("supersedes_event_id") == valid.event_id
                    and not event.payload.get("intent")]
    assert not invalidating, "a failed producer minted an empty-intent revision that supersedes the last valid decision"


def test_meta_failure_after_existing_pack_is_captured_not_an_identity_conflict(
    admitted, router, monkeypatch
):
    router["decision"] = planning._route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("synthetic established action pack")
    planning._global_a(monkeypatch, store, episode, ignition)
    planning._meta_a(monkeypatch, store, episode)
    before = {event.event_id for event in _events(episode)}
    fault = OSError("closure: second Meta A computation failed")
    downstream = []

    def fail(*_args, **_kwargs):
        raise fault

    with monkeypatch.context() as scoped:
        scoped.setattr("meta_loop.loop.intake_phase.run", fail)
        scoped.setattr(chain, "_bind_execution_contract", lambda *_a, **_k: downstream.append("contract"))
        with pytest.raises(OSError) as caught:
            chain._start_meta_a(store, episode)
    assert caught.value is fault
    assert downstream == []
    fresh = _new_events(episode, before)
    assert fresh and str(fault) in _failure_text(fresh), (
        "the later producer failure was not recorded although the journal is healthy; "
        "the successful pack's operation token was reused"
    )


def test_required_contract_producer_failure_is_recorded_before_auto_is_refused(
    admitted, monkeypatch
):
    from meta_loop import execution_contract

    store, episode, _ignition = admitted("synthetic contract producer failure")
    before = {event.event_id for event in _events(episode)}
    fault = ValueError("closure: execution contract substrate mismatch")
    downstream = []

    def fail(_episode):
        raise fault

    with monkeypatch.context() as scoped:
        scoped.setattr("pressure_loop.agent_lane.carried_program", lambda _episode: None)
        scoped.setattr(execution_contract, "motor_programs_enabled", lambda: True)
        scoped.setattr(execution_contract, "execution_contract_for", fail)
        scoped.setattr(chain, "_start_auto", lambda *_a, **_k: downstream.append("auto"))
        with pytest.raises(ValueError) as caught:
            chain._bind_execution_contract(store, episode)
    assert caught.value is fault
    assert downstream == []
    fresh = _new_events(episode, before)
    assert fresh and str(fault) in _failure_text(fresh), "the real contract binder raised with no new failure event"


@pytest.mark.parametrize("already_revised", [False, True], ids=["selection", "revision"])
def test_changed_producer_reason_is_not_silently_replayed(
    admitted, router, monkeypatch, already_revised
):
    if already_revised:
        store, episode, ignition, _first = earlier._make_revision(admitted, router, monkeypatch)
    else:
        router["decision"] = planning._route_decision("FREEFORM_CHAT")
        store, episode, ignition = admitted("synthetic reason-only change")
        planning._global_a(monkeypatch, store, episode, ignition)
    prior = earlier._intent_events(episode)[-1]
    old_reason = episode.global_["intent_decision"]["reason"]
    old_confidence = episode.global_["intent_decision"]["confidence"]
    old_intent = episode.global_["intent_decision"]["intent"]
    planning.route_context.reset()
    # Real Global A uses aggregate pressure in its reason while confidence remains
    # the unchanged dominant contributor's weight. No decision result is mocked.
    changed_ignition = dataclasses.replace(ignition, aggregate_pressure=0.625)
    try:
        planning._global_a(monkeypatch, store, episode, changed_ignition)
    except EventIdentityConflict:
        assert earlier._intent_events(episode)[-1].sealed_sha256() == prior.sealed_sha256()
        return
    produced = episode.global_["intent_decision"]
    assert produced["reason"] != old_reason, "test must change the actual producer reason"
    assert produced["intent"] == old_intent
    assert produced["confidence"] == old_confidence
    latest = earlier._intent_events(episode)[-1]
    assert latest.event_id != prior.event_id, "a new producer reason was silently replaced with the stored reason"
    assert produced["reason"] in _failure_text([latest]), "the new occurrence lost the actual producer reason"


def test_no_exported_weak_revision_writer_can_bypass_the_complete_payload(
    admitted, router, monkeypatch
):
    legacy_writer = getattr(capture, "capture_intent_revision", None)
    if not callable(legacy_writer):
        assert "capture_intent_revision" not in getattr(capture, "__all__", ())
        return  # Removing an unused weaker writer is a legitimate repair.
    router["decision"] = planning._route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("synthetic direct revision contract")
    planning._global_a(monkeypatch, store, episode, ignition)
    prior = earlier._intent_events(episode)[-1]
    try:
        revised = legacy_writer(episode, intent="RUN_AGENT", omega_lane="agent",
                                supersedes=prior.event_id, reason="synthetic explicit correction")
    except ContinuityContractError:
        return  # Refusing incomplete evidence is also legitimate.
    required = {"planning_status", "intent_confidence", "route", "router_confidence", "routing_revision"}
    assert required <= set(revised.payload), f"public revision writer lost evidence fields: {sorted(required - set(revised.payload))}"


def _exception_graph(error):
    pending = [error]
    seen = set()
    found = []
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        pending.extend([current.__cause__, current.__context__])
        pending.extend(getattr(current, "exceptions", ()))
    return found


def test_capture_failure_does_not_destroy_the_producers_existing_root_cause(
    admitted, monkeypatch
):
    store, episode, ignition = admitted("synthetic compound failure")
    root_cause = OSError("closure: original substrate root cause")
    producer_fault = RuntimeError("closure: producer wraps substrate failure")
    capture_fault = OSError("closure: recording failed independently")

    def fail_producer(*_args, **_kwargs):
        try:
            raise root_cause
        except OSError as underlying:
            raise producer_fault from underlying

    def fail_capture(*_args, **_kwargs):
        raise capture_fault

    with monkeypatch.context() as scoped:
        scoped.setattr("global_loop.loop.ignition_phase.run", fail_producer)
        scoped.setattr(capture, "capture_intent_selected", fail_capture)
        scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: pytest.fail("next phase ran"))
        with pytest.raises(RuntimeError) as caught:
            chain._start_global_a(store, episode, ignition)
    assert caught.value is producer_fault
    graph = _exception_graph(caught.value)
    assert any(error is capture_fault for error in graph), "capture failure must remain inspectable"
    assert any(error is root_cause for error in graph), "chaining the recording error destroyed the producer's existing root cause"


# Positive controls: ordinary failure propagation and exact replay remain valid.

def test_first_raised_failure_still_records_and_propagates_unchanged(
    admitted, monkeypatch
):
    store, episode, ignition = admitted("synthetic first failure positive control")
    fault = OSError("closure: first producer failure")

    def fail(*_args, **_kwargs):
        raise fault

    with monkeypatch.context() as scoped:
        scoped.setattr("global_loop.loop.ignition_phase.run", fail)
        scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: pytest.fail("next phase ran"))
        with pytest.raises(OSError) as caught:
            chain._start_global_a(store, episode, ignition)
    assert caught.value is fault
    assert str(fault) in _failure_text(_events(episode))


def test_exact_revision_retry_remains_byte_identical(admitted, router, monkeypatch):
    store, episode, ignition, _first = earlier._make_revision(admitted, router, monkeypatch)
    before = [event.to_dict() for event in _events(episode)]
    planning.route_context.reset()
    planning._global_a(monkeypatch, store, episode, ignition)
    assert [event.to_dict() for event in _events(episode)] == before
