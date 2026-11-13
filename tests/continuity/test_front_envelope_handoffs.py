"""Execution receipts and queue identity survive the front cascade handoffs.

The producers and consumers are real: AUTO execute -> fold -> episode body, and
durable queue deserialization -> admission event. Only the persona extractor and
the external skill dispatch are substituted; neither contract boundary is mocked.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_loop._auto_state import AutoInput, MemoryEnvelope, PromptPack
from auto_loop.execute.run import execute
from auto_loop.fold.run import _fold_episode_auto
from memory.cognition_capture import ADMISSION_ORIGIN, capture_admission_origin
from memory.episode_events import read_episode_events
from pressure_loop.cognition_queue import deserialize_input, serialize_input
from pressure_loop.contributors import EpisodeIgnition
from pressure_loop.episode_journal import enriched_episode_from_episode
from tests.helpers.episode_owner import owned


def _memory():
    return MemoryEnvelope(False, "skipped", None, (), (), None)


def _prompt(prompt_type="RESPONSE_ACK"):
    return PromptPack(
        prompt_type=prompt_type,
        system_prompt="",
        developer_prompt=None,
        user_prompt="Remember that the synthetic sample is amber.",
        context_blocks={},
        response_mode="ack",
        tool_call_spec=None,
        image_spec=None,
    )


def _fold_into_body(episode_id, pack, prompt, result):
    failed = bool(result.get("failure_kind"))
    goal = {"episode_id": episode_id, "intent": "RESPOND", "omega_lane": "chat"}
    folded = _fold_episode_auto(
        AutoInput(episode_id, {}, goal, pack, {}),
        status="failed" if failed else "success",
        cycle_kind="fail" if failed else "success",
        memory_envelope=_memory(),
        model_profile=None,
        model_id=None,
        prompt_pack=prompt,
        exec_result=result,
        runtime_ms=1,
    )
    carrier = SimpleNamespace(
        episode_id=episode_id,
        pipeline_id=0,
        work_kind="operator_turn",
        opened_by="operator_command",
        pressure={},
        global_={"intent_decision": goal},
        meta={"execution_action_pack": pack},
        auto=folded,
        omega={},
        brain_turns=[],
    )
    return folded, enriched_episode_from_episode(carrier)


@pytest.mark.parametrize(
    "admitted,kind",
    [
        (["synthetic:sample_color=amber"], "memory_intake_admitted"),
        ([], "memory_intake_empty"),
    ],
)
def test_actual_remember_receipt_survives_auto_fold_and_episode_body(monkeypatch, admitted, kind):
    from memory import episode_sram

    monkeypatch.setattr(episode_sram, "prepare_persona_intake_for_action", lambda text: admitted)
    episode_id = "front-remember-receipt"
    pack = {"action": "RESPONSE", "tool": None, "execution_inputs": {}}
    prompt = _prompt()
    with owned(episode_id):
        result = execute(prompt, None, pack, _memory(), episode_id, runtime_client=object())
        assert result["outputs"]["memory_intake_records"] == admitted
        assert result["outputs"]["deterministic_response_kind"] == kind
        folded, body = _fold_into_body(episode_id, pack, prompt, result)

    for outputs in (folded["outputs"], body["outputs"]):
        assert outputs.get("memory_intake_records") == admitted, (
            "the exact AUTO intake receipt must reach the episode's memory body"
        )
        assert outputs.get("deterministic_response_kind") == kind, (
            "memory intake that found nothing must stay distinguishable from admitted intake"
        )


def test_actual_skill_violation_details_survive_auto_fold_and_episode_body(monkeypatch):
    import auto_loop.skill.run as skill_run
    from core.paths import OMEGA_ROOT

    protected = (OMEGA_ROOT / "auto_loop" / "execute" / "skill_execution_guards.py").as_posix()
    monkeypatch.setattr(
        skill_run,
        "dispatch_skill_pack",
        lambda *_args, **_kwargs: {
            "status": "executed",
            "dispatch_id": "front-dispatch",
            "reason": None,
            "guard_status": {},
            "qwen_result": {
                "status": "success",
                "files_changed": [protected],
                "summary": "synthetic dispatch reported a protected write",
            },
        },
    )
    episode_id = "front-skill-violation"
    pack = {
        "action": "USE_SKILL",
        "tool": None,
        "source_action_goal": {},
        "execution_inputs": {
            "skill_execution_pack": {
                "dispatch_id": "front-dispatch",
                "episode_id": episode_id,
                "objective": "inspect the synthetic project",
                "selected_skill": {"name": "coding_skill"},
                "limits": {"max_files_changed": 5},
            }
        },
    }
    prompt = _prompt("RESPONSE_REASONING")
    with owned(episode_id):
        result = execute(prompt, None, pack, _memory(), episode_id, runtime_client=object())
        assert result["failure_kind"] == "skill_self_modification_blocked"
        violations = result["outputs"]["skill_self_mod_violations"]
        assert violations and any("skill_execution_guards.py" in str(row) for row in violations)
        folded, body = _fold_into_body(episode_id, pack, prompt, result)

    assert folded["outputs"].get("skill_self_mod_violations") == violations
    assert body["outputs"].get("skill_self_mod_violations") == violations


def test_permission_admission_records_the_queue_transport_identity():
    request_id = "a" * 32
    queue_id = "front-permission-queue-sentinel"
    contributor = deserialize_input(
        {
            "source": "operator",
            "type": "plugin_permission_decision",
            "weight": 1.0,
            "queue_id": queue_id,
            "metadata": {
                "request_id": request_id,
                "expected_revision": "b" * 64,
                "decision": "allow",
                "cycle_id": f"plugin-permission:{request_id}",
                "prior_episode_id": "prior-permission-episode",
                "task_id": "permission-task",
                "plan_id": "c" * 64,
                "plan_digest": "d" * 64,
                "arrival_seq": 4243,
                "directive_intent": "PLUGIN_OPERATION",
                "ingress_class": "authority_request",
            },
        }
    )
    assert "queue_id" not in contributor.metadata, "permission content has a closed schema"
    assert serialize_input(contributor)["queue_id"] == queue_id
    episode = SimpleNamespace(episode_id="front-permission-episode", thread_id="front-thread")
    ignition = EpisodeIgnition(
        episode_id=episode.episode_id,
        aggregate_pressure=1.0,
        opened_by=contributor.type,
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot={},
        pipeline_id=0,
        work_kind="operator_turn",
    )
    with owned(episode):
        capture_admission_origin(episode, ignition, contributor)
        origin, = [event for event in read_episode_events(episode.episode_id)
                   if event.event_kind == ADMISSION_ORIGIN]
        assert origin.payload["queue_id"] == queue_id, (
            "admission must record the same exact transport identity that the queue acknowledges"
        )
        assert origin.arrival_seq == 4243


def _contract_episode():
    episode_id = "front-execution-contract"
    goal = {"episode_id": episode_id, "intent": "USE_TOOL", "omega_lane": "chat"}
    pack = {
        "episode_id": episode_id,
        "action": "USE_TOOL",
        "tool": "web_fetch",
        "tool_reason": "read the requested source",
        "source_action_goal": deepcopy(goal),
        "execution_intent": "read a synthetic source",
        "execution_inputs": {"url": "https://example.org/approved", "path": "approved.txt"},
        "execution_constraints": {
            "planning_only": True,
            "no_inference_in_meta": True,
            "no_execution_in_meta": True,
            "no_model_fields": True,
        },
        "memory_required": False,
        "memory_answer_required": False,
        "memory_reason": None,
        "memory_query": None,
        "memory_scope": [],
        "memory_tools": [],
    }
    return SimpleNamespace(
        episode_id=episode_id,
        pipeline_id=0,
        work_kind="operator_turn",
        opened_by="operator_command",
        thread_id="front-contract-thread",
        pressure={},
        global_={"intent_decision": goal},
        meta={"execution_action_pack": pack},
    )


def _fold_input_on_episode(episode, auto_input):
    episode.auto = _fold_episode_auto(
        auto_input,
        status="success",
        cycle_kind="success",
        memory_envelope=_memory(),
        model_profile=None,
        model_id=None,
        prompt_pack=_prompt(),
        exec_result={"outputs": {}},
        runtime_ms=1,
    )


def _consistency(episode):
    from meta_loop.consistency.run import meta_b_input_from_episode, run

    return run(meta_b_input_from_episode(episode))


@pytest.mark.parametrize(
    "field,replacement",
    [("url", "https://example.org/unapproved"), ("path", "different.txt")],
)
def test_meta_b_rejects_different_executed_arguments_for_the_same_tool(field, replacement):
    from auto_loop.intake.run import auto_input_from_episode

    episode = _contract_episode()
    planned = deepcopy(episode.meta["execution_action_pack"])
    auto_input = auto_input_from_episode(episode)
    auto_input.execution_pack["execution_inputs"][field] = replacement
    _fold_input_on_episode(episode, auto_input)

    consistency, critical = _consistency(episode)

    assert episode.meta["execution_action_pack"] == planned, "AUTO must have an independent pack"
    assert critical is True, "identical tool names do not excuse a changed execution target"
    assert consistency["pack_matches"] is False


def test_auto_fold_preserves_the_full_global_source_independently_of_the_meta_projection():
    from auto_loop.intake.run import auto_input_from_episode

    episode = _contract_episode()
    episode.global_["intent_decision"]["source_evidence"] = {
        "request_ids": ["front-source-authority"],
    }
    source = deepcopy(episode.global_["intent_decision"])
    projection = deepcopy(episode.meta["execution_action_pack"]["source_action_goal"])
    auto_input = auto_input_from_episode(episode)
    _fold_input_on_episode(episode, auto_input)

    assert episode.auto["source_action_goal"] == source
    assert episode.auto["execution_pack"]["source_action_goal"] == projection
    auto_input.source_action_goal["source_evidence"]["request_ids"].append("later-change")
    assert episode.global_["intent_decision"] == source
    assert episode.auto["source_action_goal"] == source


def test_meta_b_rejects_same_episode_outer_global_source_drift():
    from auto_loop.intake.run import auto_input_from_episode

    episode = _contract_episode()
    auto_input = auto_input_from_episode(episode)
    _fold_input_on_episode(episode, auto_input)
    episode.auto["source_action_goal"] = {
        **deepcopy(episode.global_["intent_decision"]),
        "intent": "DOCUMENT_CREATE",
    }

    consistency, critical = _consistency(episode)

    assert critical is True, "the outer Global A source is independent execution provenance"
    assert consistency["pack_matches"] is False


@pytest.mark.parametrize(
    "field,empty",
    [("source_action_goal", False), ("source_action_goal", True), ("execution_pack", False)],
)
def test_meta_b_does_not_reconstruct_missing_auto_evidence_from_the_expected_plan(field, empty):
    from auto_loop.intake.run import auto_input_from_episode

    episode = _contract_episode()
    _fold_input_on_episode(episode, auto_input_from_episode(episode))
    if empty:
        episode.auto[field] = {}
    else:
        episode.auto.pop(field)

    consistency, critical = _consistency(episode)

    assert critical is True, "missing execution evidence cannot be replaced by expected evidence"
    assert consistency["pack_matches"] is False


def test_auto_validation_refusal_retains_its_real_source_without_fabricating_execution(monkeypatch):
    import auto_loop.loop as auto_loop

    episode = _contract_episode()
    monkeypatch.setattr(
        auto_loop.validate_phase, "validate_for_execution",
        lambda _pack: (False, "synthetic_refusal", "the selected action is not executable"),
    )
    with owned(episode):
        outcome = auto_loop.run_auto(episode, runtime_client=object())

    assert outcome is not None
    assert episode.auto["status"] == "failed"
    assert episode.auto["failure_kind"] == "synthetic_refusal"
    assert episode.auto["outputs"] == {}
    assert episode.auto.get("source_action_goal") == episode.global_["intent_decision"]
    consistency, critical = _consistency(episode)
    assert critical is False, "a refusal still reports the source it actually validated"
    assert consistency["pack_matches"] is True


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("execution_constraints", {"planning_only": False}),
        ("memory_required", True),
        ("memory_tools", [{"tool": "remember_identity", "args": {}}]),
    ],
)
def test_meta_b_rejects_execution_authority_and_memory_contract_drift(field, replacement):
    from auto_loop.intake.run import auto_input_from_episode

    episode = _contract_episode()
    auto_input = auto_input_from_episode(episode)
    auto_input.execution_pack[field] = replacement
    _fold_input_on_episode(episode, auto_input)

    consistency, critical = _consistency(episode)

    assert critical is True
    assert consistency["pack_matches"] is False


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("episode_id", "a-different-source-episode"),
        ("intent", "DOCUMENT_CREATE"),
        ("target", {"path": "a-different-source-target.txt"}),
    ],
)
def test_meta_b_rejects_changed_source_action_provenance(field, replacement):
    from auto_loop.intake.run import auto_input_from_episode

    episode = _contract_episode()
    goal = episode.global_["intent_decision"]
    goal["target"] = {"path": "approved-source-target.txt"}
    episode.meta["execution_action_pack"]["source_action_goal"] = deepcopy(goal)
    original = deepcopy(goal)
    auto_input = auto_input_from_episode(episode)
    auto_input.execution_pack["source_action_goal"][field] = replacement
    _fold_input_on_episode(episode, auto_input)

    consistency, critical = _consistency(episode)

    assert episode.meta["execution_action_pack"]["source_action_goal"] == original
    assert critical is True, "the executed pack must preserve the action's authoritative source"
    assert consistency["pack_matches"] is False


def test_meta_b_accepts_admitted_job_invocation_restored_by_auto_intake():
    from auto_loop.intake.run import auto_input_from_episode

    episode = _contract_episode()
    invocation = {"job_id": "job-front-sentinel", "invocation_id": "invocation-front-sentinel"}
    episode.pressure = {"contributors": [{"detail": {"job_invocation": invocation}}]}
    original = deepcopy(episode.meta["execution_action_pack"])
    auto_input = auto_input_from_episode(episode)
    assert auto_input.execution_pack["execution_inputs"]["job_invocation"] == invocation
    assert "job_invocation" not in original["execution_inputs"]
    _fold_input_on_episode(episode, auto_input)

    consistency, critical = _consistency(episode)

    assert critical is False, "the admitted invocation is an authorized intake transformation"
    assert consistency["pack_matches"] is True
    assert episode.meta["execution_action_pack"] == original


def test_meta_b_accepts_motor_program_canonicalization_without_ignoring_its_content():
    from auto_loop.intake.run import auto_input_from_episode
    from schema.motor_program import MotorProgram

    episode = _contract_episode()
    fixture = Path(__file__).resolve().parents[1] / "fixtures/motor_programs/derive_implement_verify.json"
    program = MotorProgram.from_dict({
        **json.loads(fixture.read_text(encoding="utf-8")),
        "episode_id": episode.episode_id,
    })
    raw = program.to_dict()
    episode.meta["motor_program"] = deepcopy(raw)
    episode.meta["execution_action_pack"]["execution_inputs"]["motor_program"] = deepcopy(raw)
    original = deepcopy(episode.meta["execution_action_pack"])
    auto_input = auto_input_from_episode(episode)
    canonical = auto_input.execution_pack["execution_inputs"]["motor_program"]
    assert "fingerprint" not in raw
    assert canonical["fingerprint"] == program.fingerprint()
    _fold_input_on_episode(episode, auto_input)

    consistency, critical = _consistency(episode)

    assert critical is False, "AUTO's explicit canonical representation preserves the same program"
    assert consistency["pack_matches"] is True
    assert episode.meta["execution_action_pack"] == original

    episode.auto["execution_pack"]["execution_inputs"]["motor_program"]["pathway"]["method"] = (
        "a different method the source program never authorized"
    )
    _, critical = _consistency(episode)
    assert critical is True, "canonicalization must not normalize away changed executed content"


def test_meta_b_accepts_browser_memory_normalization_without_ignoring_later_drift(monkeypatch):
    import auto_loop.loop as auto_loop

    episode = _contract_episode()
    pack = episode.meta["execution_action_pack"]
    pack.update(
        action="RESPONSE",
        tool=None,
        memory_required=True,
        memory_answer_required=True,
        memory_reason="stale broad classifier",
        memory_query="stale query",
        memory_scope=["identity"],
        memory_tools=[{"tool": "remember_identity", "args": {}}],
    )
    pack["execution_inputs"]["operator_route"] = "browser"
    pack["source_action_goal"]["intent"] = "RESPOND"
    episode.global_["intent_decision"]["intent"] = "RESPOND"
    original = deepcopy(pack)
    monkeypatch.setattr(
        auto_loop.execute_phase,
        "execute",
        lambda *_args, **_kwargs: {"outputs": {"response_text": "synthetic browser receipt"}},
    )

    with owned(episode):
        outcome = auto_loop.run_auto(episode, runtime_client=object())
        assert outcome is not None and episode.auto["status"] == "success"
        actual = episode.auto["execution_pack"]
        assert actual["memory_required"] is False
        assert actual["memory_tools"] == []
        consistency, critical = _consistency(episode)
        assert critical is False
        assert consistency["pack_matches"] is True
        assert episode.meta["execution_action_pack"] == original

        actual["memory_required"] = True
        _, critical = _consistency(episode)
        assert critical is True, "expected browser normalization cannot excuse mutated execution"


def _resume_episode():
    return SimpleNamespace(
        episode_id="front-dev-resume",
        thread_id="front-dev-thread",
        meta={"agent_lane_dispatch": {
            "motor_program": {"outcome_contract": {"objective": "resume the synthetic build"}},
        }},
    )


def test_dev_recall_failure_preserves_its_cause_and_records_the_failed_episode_fact(monkeypatch):
    from pressure_loop.agent_lane import recall_dev_context

    cause = OSError("dev-recall-SENTINEL: graph storage unavailable")
    fault = RuntimeError("dev-recall-SENTINEL: unable to read build history")

    def unavailable(_spec):
        raise fault from cause

    monkeypatch.setattr("memory.api.read_memory", unavailable)
    episode = _resume_episode()
    with owned(episode):
        with pytest.raises(RuntimeError) as caught:
            recall_dev_context(episode)
        assert caught.value is fault
        assert caught.value.__cause__ is cause
        events = read_episode_events(episode.episode_id)
        assert any(str(fault) in json.dumps(event.to_dict()) for event in events), (
            "failed recall must become episode evidence before it propagates"
        )


def test_dev_recall_that_ran_and_found_no_history_remains_a_legitimate_empty_result(monkeypatch):
    from pressure_loop.agent_lane import recall_dev_context

    monkeypatch.setattr("memory.api.read_memory", lambda _spec: SimpleNamespace(candidates=[]))
    episode = _resume_episode()
    with owned(episode):
        assert recall_dev_context(episode) is None
        assert not any("dev_recall_failed" in event.event_kind
                       for event in read_episode_events(episode.episode_id))


def test_admitted_continuation_recall_failure_reaches_canonical_memory(monkeypatch, tmp_path):
    from core.cognition_lock import release_held_locks_for_tests
    from memory import episode_commit_view, episode_sram
    from memory.engine import walker
    from pressure_loop import agent_lane, cognition_queue
    from pressure_loop.contributors import make_contributor
    from schema.episode_record import EpisodeRecord
    from tests.core.test_agent_lane import _envelope
    from tests.helpers.blueprint_driver import BlueprintDriver

    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    continuation = agent_lane.build_contributor(
        _envelope(), contributor_type=agent_lane.AGENT_CONTINUATION
    )
    assert cognition_queue.enqueue_input(continuation, operator_activity=False)
    leased = cognition_queue.lease_next_input(2)
    assert leased is not None
    waiting = make_contributor("operator", "operator_query", 1.0, {
        "message": "The next independent synthetic request.",
        "cycle_id": "front-waiting-cycle",
    })
    assert cognition_queue.enqueue_input(waiting, operator_activity=False)
    cause = OSError("continuation-recall-SENTINEL: graph storage unavailable")
    fault = RuntimeError("continuation-recall-SENTINEL: build history could not be read")

    def unavailable(_spec):
        raise fault from cause

    monkeypatch.setattr("memory.api.read_memory", unavailable)
    opened = []
    real_open = driver.store.open

    def observe_open(*args, **kwargs):
        episode = real_open(*args, **kwargs)
        opened.append(episode)
        return episode

    monkeypatch.setattr(driver.store, "open", observe_open)
    try:
        with pytest.raises(RuntimeError) as caught:
            driver._ignite(leased, opened_by=leased.type)
        assert caught.value is fault
        assert caught.value.__cause__ is cause
        assert len(opened) == 1
        episode = opened[0]
        assert driver.generations == [], "recall failed before any execution or model pass"
        assert cognition_queue.queue_depth() == 1
        remaining = cognition_queue.lease_next_input(0)
        assert remaining is not None
        assert remaining.metadata["cycle_id"] == "front-waiting-cycle"
        assert driver.store.current() is None, "post-open failure still owes the episode its terminus"
        assert not episode_sram.is_open(episode.episode_id)
        ref = episode_commit_view.episode_record_ref(episode.episode_id)
        assert ref is not None, "failure must be published by DeepSleep before propagation"
        walker.load_coord_index()
        assert ref.record_id in walker.COORD_INDEX
        document = json.loads(Path(walker.path_for_id(ref.record_id)).read_text(encoding="utf-8"))
        record = EpisodeRecord.from_dict(json.loads(document["text"])["episode_record"])
        assert record.outcome.cascade_status == "failed"
        failure = next(event for event in record.events["by_value"]
                       if event.event_kind == "admission.dev_recall_failed")
        assert failure.episode_id == episode.episode_id
        assert str(fault) in json.dumps(failure.to_dict())
        origin = next(event for event in record.events["by_value"]
                      if event.event_kind == "admission.origin")
        assert origin.payload["queue_id"] == leased.metadata["queue_id"]
    finally:
        # A fail-hard runtime exits with its substrate lock held. This in-process
        # fault injection must hand it back even while the regression is RED.
        release_held_locks_for_tests()
        episode_sram.reset()
