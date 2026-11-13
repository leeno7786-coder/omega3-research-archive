"""WO-04A r2 review: distinct failure invocations and complete exception branches.

Install under tests/continuity. External scratch execution must load tests.conftest
and tests.continuity.conftest as plugins. No production state or live model is used.
"""
from __future__ import annotations

import pytest

import cognition_chain as chain
from memory import cognition_capture as capture
from memory.episode_events import read_episode_events, record_event
from tests.continuity import test_planning_capture as planning
from tests.continuity import test_wo04a_closure_regressions as previous

admitted = planning.admitted
router = planning.router
_fresh_route_context = planning._fresh_route_context


def _events(episode):
    return list(read_episode_events(episode.episode_id))


@pytest.mark.parametrize("phase", ["global_a", "meta_a", "contract"])
def test_distinct_failed_producer_invocations_are_not_deduplicated_by_text(
    admitted, router, monkeypatch, phase
):
    """The producer really runs twice; neither call is a persistence-only retry."""
    router["decision"] = planning._route_decision("FREEFORM_CHAT")
    store, episode, ignition = admitted("synthetic repeated-error occurrence test")
    planning._global_a(monkeypatch, store, episode, ignition)
    if phase != "global_a":
        planning._meta_a(monkeypatch, store, episode)
    before = {event.event_id: event.to_dict() for event in _events(episode)}
    faults = []
    reached = []

    def fail(*_args, **_kwargs):
        # New exception object from each separately executed producer invocation.
        fault = OSError("the same diagnostic on two actual invocations")
        faults.append(fault)
        raise fault

    with monkeypatch.context() as scoped:
        if phase == "global_a":
            scoped.setattr("global_loop.loop.ignition_phase.run", fail)
            scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: reached.append("meta"))

            def invoke():
                chain._start_global_a(store, episode, ignition)
        elif phase == "meta_a":
            scoped.setattr("meta_loop.loop.intake_phase.run", fail)
            scoped.setattr(chain, "_bind_execution_contract", lambda *_a, **_k: reached.append("contract"))

            def invoke():
                chain._start_meta_a(store, episode)
        else:
            from meta_loop import execution_contract

            scoped.setattr("pressure_loop.agent_lane.carried_program", lambda _episode: None)
            scoped.setattr(execution_contract, "motor_programs_enabled", lambda: True)
            scoped.setattr(execution_contract, "execution_contract_for", fail)
            scoped.setattr(chain, "_start_auto", lambda *_a, **_k: reached.append("auto"))

            def invoke():
                chain._bind_execution_contract(store, episode)

        for _ in range(2):
            with pytest.raises(OSError) as caught:
                invoke()
            assert caught.value is faults[-1]

    assert len(faults) == 2 and faults[0] is not faults[1], "both producers must actually execute"
    assert str(faults[0]) == str(faults[1])
    assert reached == [], "a failure must never authorize the next phase"
    after = _events(episode)
    assert {event.event_id: event.to_dict() for event in after if event.event_id in before} == before
    failed = [event for event in after
              if event.event_id not in before and event.payload.get("planning_status") == "failed"]
    assert len(failed) == 2, (
        f"{phase}: producer executed twice but {len(failed)} failed occurrence(s) survive; "
        "equal error text does not establish an unchanged persistence retry"
    )
    assert failed[0].event_id != failed[1].event_id
    assert failed[0].event_seq < failed[1].event_seq


def test_recording_errors_implicit_root_context_survives_compound_failure(
    admitted, monkeypatch
):
    """Preserve BOTH fault trees, including a recording error's implicit context."""
    store, episode, ignition = admitted("synthetic two rooted failure branches")
    producer_root = OSError("the original producer substrate error")
    producer_fault = RuntimeError("producer wraps the original substrate error")
    recording_root = ValueError("the recording operation itself failed internally")
    recording_fault = OSError("recording wrapped its own internal failure")

    def fail_producer(*_args, **_kwargs):
        try:
            raise producer_root
        except OSError as underlying:
            raise producer_fault from underlying

    def fail_recording(*_args, **_kwargs):
        try:
            raise recording_root
        except ValueError:
            # Implicit exception context is legitimate, not a missing root cause.
            raise recording_fault

    with monkeypatch.context() as scoped:
        scoped.setattr("global_loop.loop.ignition_phase.run", fail_producer)
        scoped.setattr(capture, "capture_intent_selected", fail_recording)
        scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: pytest.fail("next phase ran"))
        with pytest.raises(RuntimeError) as caught:
            chain._start_global_a(store, episode, ignition)
    assert caught.value is producer_fault
    graph = previous._exception_graph(caught.value)
    assert any(error is producer_root for error in graph), "producer root must survive"
    assert any(error is recording_fault for error in graph), "recording failure must survive"
    assert any(error is recording_root for error in graph), (
        "the handler overwrote the recording error's pre-existing implicit root context"
    )


# Positive controls: persistence replay and the previously repaired fault shape.
def test_explicit_operation_tokens_distinguish_execution_from_persistence_retry(admitted):
    """The accepted event API already supports distinct occurrences with equal payloads."""
    _store, episode, _ignition = admitted("synthetic operation identity positive control")
    payload = {"planning_status": "failed", "reason": "identical diagnostic"}
    first = record_event(episode.episode_id, operation_id="r3-first-invocation", phase="global_a",
                         event_kind="global_a.phase_failed", payload=payload)
    second = record_event(episode.episode_id, operation_id="r3-second-invocation", phase="global_a",
                          event_kind="global_a.phase_failed", payload=payload)
    retry = record_event(episode.episode_id, operation_id="r3-first-invocation", phase="global_a",
                         event_kind="global_a.phase_failed", payload=payload)
    assert first.event_id != second.event_id
    assert first.event_seq < second.event_seq
    assert retry.to_dict() == first.to_dict()
    assert sum(event.event_id == first.event_id for event in _events(episode)) == 1


def test_direct_recording_failure_keeps_the_previously_repaired_producer_root(
    admitted, monkeypatch
):
    store, episode, ignition = admitted("synthetic direct recording-failure positive control")
    producer_root = OSError("producer root")
    producer_fault = RuntimeError("producer failure")
    recording_fault = OSError("direct recording failure")

    def fail_producer(*_args, **_kwargs):
        try:
            raise producer_root
        except OSError as underlying:
            raise producer_fault from underlying

    def fail_recording(*_args, **_kwargs):
        raise recording_fault

    with monkeypatch.context() as scoped:
        scoped.setattr("global_loop.loop.ignition_phase.run", fail_producer)
        scoped.setattr(capture, "capture_intent_selected", fail_recording)
        scoped.setattr(chain, "_start_meta_a", lambda *_a, **_k: pytest.fail("next phase ran"))
        with pytest.raises(RuntimeError) as caught:
            chain._start_global_a(store, episode, ignition)
    assert caught.value is producer_fault
    graph = previous._exception_graph(caught.value)
    assert any(error is producer_root for error in graph)
    assert any(error is recording_fault for error in graph)
