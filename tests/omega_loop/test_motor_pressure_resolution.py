from __future__ import annotations

from types import SimpleNamespace

import pytest

import cognition_chain as chain
from memory import episode_sram
from omega_loop.episode.prompts import (
    _parse_apply,
    _parse_knowledge,
    _parse_pressure_lifecycle,
    prompt_spec_for,
)
from omega_loop.episode.packet import attach_omega_packet, build_omega_episode_input
from omega_loop.episode.run import _packet_from_episode
from omega_loop.episode.types import OmegaEpisodeInput, OmegaEpisodeResult
from global_loop.assemble.run import GlobalBInput
from pressure_loop.emergent_pressure import (
    resolve_emergent_pressure,
    stage_emergent_pressure,
)
from schema.emergent_pressure import EmergentPressure
from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import MotorProgram


def _packet(*, action: str = "EXECUTE_MOTOR_PROGRAM") -> OmegaEpisodeInput:
    return OmegaEpisodeInput(
        episode_id="ep1",
        intent="PURSUE_OUTCOME",
        action=action,
        tool=None,
        domain="software_engineering",
        operator_text=None,
        outcome="failure",
        failure_reason="implementation evidence missing",
        raw_tool_result={},
        recall=(),
        recall_error=None,
        prior_mastery=None,
        applied={"status": "failure"},
        motor_program=_program_dict(),
        originating_pressure=_origin_pressure_dict(),
    )


def _non_motor_packet(*, action: str = "LEARN") -> OmegaEpisodeInput:
    """A LEARN/USE_TOOL-style episode that never authored a canonical motor program."""
    return OmegaEpisodeInput(
        episode_id="ep1",
        intent="ACQUIRE_KNOWLEDGE",
        action=action,
        tool=None,
        domain="algorithms",
        operator_text=None,
        outcome="success",
        failure_reason=None,
        raw_tool_result={},
        recall=(),
        recall_error=None,
        prior_mastery=None,
        applied={},
        motor_program=None,
        originating_pressure=None,
    )


def test_non_motor_fold_drops_pressure_resolution_instead_of_failing():
    """A non-motor episode that emits a pressure_resolution must DROP it (return
    None, None) — mirroring the deterministic consumer (_persist_motor_attempt_
    and_pressure) which ignores the lifecycle when there is no canonical motor
    program. It must NOT hard-fail the whole fold. Cure for the live failure
    'pressure lifecycle requires the current canonical motor program' on
    LEARN/USE_TOOL folds."""
    packet = _non_motor_packet()
    payload = {
        "pressure_resolution": {"pressure_id": "p1", "status": "satisfied", "reason": "done"},
        "emergent_pressure": None,
    }
    resolution, successor = _parse_pressure_lifecycle(payload, packet)
    assert resolution is None
    assert successor is None


def _pressure_dict() -> dict:
    return {
        "pressure_id": "p2",
        "name": "implementation_evidence_gap",
        "source_episode_id": "ep1",
        "outcome_id": "out1",
        "desired_state": {"tests": "passing"},
        "observed_state": {"tests": "absent"},
        "gap_description": "No executable implementation exists",
        "evidence": ["mp1", "pytest:failed"],
        "failed_program_ids": ["mp1"],
        "urgency": 0.9,
        "persistence": 1,
        "proposed_action": {
            "intended_transformation": "turn the design into verified code",
            "candidate_pathway": "derive-implement-verify",
            "required_capabilities": ["code_execution", "testing"],
        },
        "activation_conditions": ["implementation evidence is absent"],
        "resolution_conditions": ["required tests pass"],
        "supersedes_pressure_id": "p1",
    }


def _program_dict() -> dict:
    return {
        "program_id": "mp1",
        "parent_program_id": "",
        "outcome_id": "out1",
        "episode_id": "ep1",
        "attempt_number": 1,
        "created_at": "2026-07-10T00:00:00+00:00",
        "outcome_contract": {
            "objective": "implement and verify",
            "acceptance_criteria": ["pytest passes"],
        },
        "pathway": {
            "pathway_id": "old-path",
            "revision": 1,
            "name": "old path",
            "method": "attempt implementation",
            "operational_rationale": "tests the current hypothesis",
        },
        "execution_plan": {
            "capability_class": "code_execution",
            "operations": ["implement", "run tests"],
        },
        "evidence_contract": {
            "required_checks": ["pytest"],
            "completion_signals": ["tests pass"],
        },
    }


def _origin_pressure_dict() -> dict:
    pressure = _pressure_dict()
    pressure.update(
        {
            "pressure_id": "p1",
            "name": "original_implementation_gap",
            "source_episode_id": "ep0",
            "failed_program_ids": ["mp0"],
            "supersedes_pressure_id": "",
        }
    )
    pressure["proposed_action"] = {
        "intended_transformation": "attempt an implementation",
        "candidate_pathway": "old-path",
        "required_capabilities": ["code_execution", "testing"],
    }
    return pressure


def _attempt_identity():
    from memory.motor_experience import motor_attempt_identity

    return motor_attempt_identity(
        MotorProgram.from_dict(_program_dict()),
        ExecutionEvidence.from_dict(
            {
                "program_id": "mp1",
                "episode_id": "ep1",
                "status": "failure",
                "files_changed": ["src/example.py"],
                "tests_run": ["pytest -q"],
                "verification_results": ["1 failed"],
            }
        ),
    )


def _reflection_payload() -> dict:
    return {
        "knowledge_to_ingest": [],
        "pressure_params": {},
        "pressure_resolution": {
            "pressure_id": "p1",
            "status": "supersede",
            "reason": "the attempted method failed verification",
        },
        "emergent_pressure": _pressure_dict(),
    }


def test_episode_reflection_validates_complete_executable_pressure():
    result = _parse_knowledge(_reflection_payload(), _packet())
    assert result.pressure_resolution == {
        "pressure_id": "p1",
        "status": "supersede",
        "reason": "the attempted method failed verification",
    }
    assert result.emergent_pressure == _pressure_dict()


def test_initial_motor_attempt_can_create_first_successor_pressure():
    packet = _packet()
    packet = OmegaEpisodeInput(
        **{
            **packet.__dict__,
            "originating_pressure": None,
        }
    )
    successor = _pressure_dict()
    successor["supersedes_pressure_id"] = ""
    payload = {
        "knowledge_to_ingest": [],
        "pressure_params": {},
        "pressure_resolution": {
            "pressure_id": "",
            "status": "create",
            "reason": "initial retrieval exposed a technical depth gap",
        },
        "emergent_pressure": successor,
    }

    result = _parse_knowledge(payload, packet)

    assert result.pressure_resolution["status"] == "create"
    assert result.emergent_pressure == successor


def test_create_pressure_cannot_replace_an_existing_origin():
    successor = _pressure_dict()
    successor["supersedes_pressure_id"] = ""
    payload = {
        "knowledge_to_ingest": [],
        "pressure_params": {},
        "pressure_resolution": {
            "pressure_id": "",
            "status": "create",
            "reason": "must not bypass revision continuity",
        },
        "emergent_pressure": successor,
    }
    with pytest.raises(ValueError, match="create.*no originating pressure"):
        _parse_knowledge(payload, _packet())


def test_apply_reflection_uses_the_same_pressure_contract():
    result = _parse_apply(_reflection_payload(), _packet(action="APPLY"))
    assert result.emergent_pressure == _pressure_dict()


@pytest.mark.parametrize(
    "lifecycle",
    [
        {
            "pressure_resolution": {
                "pressure_id": "p1",
                "status": "satisfied",
                "reason": "the requested work is complete",
            },
            "emergent_pressure": None,
        },
        {
            "pressure_resolution": {
                "pressure_id": "",
                "status": "create",
                "reason": "more work may be useful",
            },
            "emergent_pressure": {"name": "incomplete model-authored suggestion"},
        },
    ],
)
def test_apply_fold_drops_inapplicable_optional_pressure_lifecycle(lifecycle):
    """Optional lifecycle prose must not destroy a valid execution foldback."""
    base = _packet(action="APPLY")
    packet = OmegaEpisodeInput(**{**base.__dict__, "originating_pressure": None})

    result = _parse_apply({"pressure_params": {}, **lifecycle}, packet)

    assert result.cognized is True
    assert result.pressure_resolution is None
    assert result.emergent_pressure is None


def _with_done_control(payload: dict) -> dict:
    assessment = dict.fromkeys(
        ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
        "attempt evidence",
    )
    control = {"assessment": {**assessment, "gap": "", "evidence": ["pytest:failed"]},
               "decision": "DONE", "pressure": None, "synthesis": "attempt reviewed"}
    return {**payload, "control": control}


def test_motor_attempt_continuity_is_rendered_as_evidence_not_authored_lifecycle():
    """Omega must see the exact program and pressure identities it judges, but the
    control envelope is the only pressure authority: a model-authored lifecycle
    never crosses a lane into the result (plan Task 13)."""
    spec = prompt_spec_for(_packet())
    system, user = spec.build(_packet())
    rendered = system + user
    assert '"program_id": "mp1"' in rendered
    assert '"outcome_id": "out1"' in rendered
    assert '"pressure_id": "p1"' in rendered
    assert "pressure_resolution" not in system
    result = spec.parse(_with_done_control(_reflection_payload()), _packet())
    assert (result.pressure_resolution, result.emergent_pressure) == (None, None)


def test_motor_skill_transport_reaches_foldback_as_applied_evidence(monkeypatch):
    monkeypatch.setattr("omega_loop.episode.packet.load_mastery", lambda: {})
    packet = build_omega_episode_input(
        GlobalBInput(
            episode_id="ep1",
            pressure={},
            source_action_goal={"intent": "PURSUE_OUTCOME"},
            execution_pack={
                "action": "EXECUTE_MOTOR_PROGRAM",
                "execution_inputs": {
                    "motor_transport": "USE_SKILL",
                    "domain": "software_engineering",
                },
            },
            auto_outcome={
                "auto": {
                    "outputs": {
                        "skill_status": "failure",
                        "skill_result": {
                            "status": "failure",
                            "summary": "test failed",
                            "files_changed": ["src/example.py"],
                            "tests_run": ["pytest -q"],
                        },
                    }
                }
            },
            meta_reflection={},
            trajectory_snapshot={},
        ),
        {"execution": {"result": "failure", "failure_reason": "test failed"}},
        motor_program=_program_dict(),
    )
    assert packet.action == "EXECUTE_MOTOR_PROGRAM"
    assert packet.applied["status"] == "failure"
    assert packet.applied["files"] == ["example.py"]
    assert packet.motor_program == MotorProgram.from_dict(_program_dict()).to_dict()
    assert packet.originating_pressure is None


def test_live_packet_carries_exact_program_and_originating_pressure(monkeypatch):
    monkeypatch.setattr("omega_loop.episode.packet.load_mastery", lambda: {})
    gbi = GlobalBInput(
        episode_id="ep1",
        pressure={},
        source_action_goal={"intent": "PURSUE_OUTCOME"},
        execution_pack={
            "action": "EXECUTE_MOTOR_PROGRAM",
            "execution_inputs": {
                "motor_transport": "USE_SKILL",
                "domain": "software_engineering",
                "emergent_pressure": _origin_pressure_dict(),
            },
        },
        auto_outcome={"auto": {"outputs": {}}},
        meta_reflection={},
        trajectory_snapshot={},
    )
    episode = SimpleNamespace(meta={"motor_program": _program_dict()}, global_={})
    packet = attach_omega_packet(
        episode, gbi, {"execution": {"result": "failure", "failure_reason": "failed"}}
    )
    rebuilt = _packet_from_episode(episode)
    assert packet.motor_program == MotorProgram.from_dict(_program_dict()).to_dict()
    assert (
        packet.originating_pressure == EmergentPressure.from_dict(_origin_pressure_dict()).to_dict()
    )
    assert rebuilt == packet
    spec = prompt_spec_for(rebuilt)
    system, user = spec.build(rebuilt)
    assert '"pressure_id": "p1"' in system + user
    result = spec.parse(_with_done_control(_reflection_payload()), rebuilt)
    assert (result.pressure_resolution, result.emergent_pressure) == (None, None)


def test_packet_rejects_conflicting_program_representations_before_reflection(monkeypatch):
    monkeypatch.setattr("omega_loop.episode.packet.load_mastery", lambda: {})
    conflicting = _program_dict()
    conflicting["program_id"] = "invented"
    gbi = GlobalBInput(
        episode_id="ep1",
        pressure={},
        source_action_goal={"intent": "PURSUE_OUTCOME"},
        execution_pack={
            "action": "EXECUTE_MOTOR_PROGRAM",
            "execution_inputs": {
                "motor_transport": "USE_SKILL",
                "domain": "software_engineering",
                "motor_program": conflicting,
                "emergent_pressure": _origin_pressure_dict(),
            },
        },
        auto_outcome={"auto": {"outputs": {}}},
        meta_reflection={},
        trajectory_snapshot={},
    )
    with pytest.raises(ValueError, match="conflicting motor program"):
        build_omega_episode_input(gbi, {}, motor_program=_program_dict())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload["pressure_resolution"].update(pressure_id="invented"),
            "originating pressure",
        ),
        (lambda payload: payload["emergent_pressure"].update(outcome_id="invented"), "outcome_id"),
        (
            lambda payload: payload["emergent_pressure"].update(source_episode_id="invented"),
            "source_episode_id",
        ),
        (
            lambda payload: payload["emergent_pressure"].update(failed_program_ids=["mp0"]),
            "failed_program_ids",
        ),
        (
            lambda payload: payload["emergent_pressure"].update(supersedes_pressure_id="invented"),
            "supersedes",
        ),
    ],
)
def test_model_authored_continuity_ids_are_rejected_and_never_cross_a_lane(mutation, message):
    payload = _reflection_payload()
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        _parse_knowledge(payload, _packet())
    result = prompt_spec_for(_packet()).parse(_with_done_control(payload), _packet())
    assert (result.pressure_resolution, result.emergent_pressure) == (None, None)


def test_revision_status_requires_a_complete_canonical_pressure():
    payload = _reflection_payload()
    del payload["emergent_pressure"]["activation_conditions"]
    with pytest.raises(ValueError, match="activation_conditions"):
        _parse_knowledge(payload, _packet())


def test_continue_cannot_silently_change_pathway_identity():
    payload = _reflection_payload()
    payload["pressure_resolution"]["status"] = "continue"
    with pytest.raises(ValueError, match="continue.*pathway"):
        _parse_knowledge(payload, _packet())


def test_pressure_intake_is_accepted_and_never_staged(monkeypatch):
    import pressure_loop.emergent_pressure as emergent_pressure

    calls = []
    episode_sram.reset()
    episode_sram.open_bucket("ep1", pipeline_id=0)
    real_intake = emergent_pressure.intake_memory

    def _record_intake(env, **kwargs):
        receipt = real_intake(env, **kwargs)
        calls.append(("intake", env.coord, env.type, kwargs, receipt.validation_status))
        return receipt

    monkeypatch.setattr(emergent_pressure, "intake_memory", _record_intake)

    try:
        contributor = stage_emergent_pressure(
            EmergentPressure.from_dict(_pressure_dict()),
            _attempt_identity(),
            originating_pressure=EmergentPressure.from_dict(_origin_pressure_dict()),
        )
    finally:
        episode_sram.reset()

    assert calls[0] == (
        "intake",
        [4, 4, 0],
        "emergent_pressure",
        {"propose": False, "bypass_guardian": True},
        "held",
    )
    # Exactly ONE call: the canonical write. The staging that used to follow it
    # minted a rival drive out of a closing fold (operator 2026-08-15: "there
    # should be no minted anything for pressure").
    assert len(calls) == 1
    assert contributor.source == "omega"
    assert contributor.metadata["emergent_pressure"] == _pressure_dict()


def test_resolution_is_a_superseding_pressure_record_in_sram():
    resolution = {"pressure_id": "p1", "status": "supersede", "reason": "method failed"}

    episode_sram.reset()
    episode_sram.open_bucket("ep1", pipeline_id=0)
    try:
        receipt = resolve_emergent_pressure(
            "p1",
            resolution,
            _attempt_identity(),
            originating_pressure=EmergentPressure.from_dict(_origin_pressure_dict()),
            provenance_refs=["memory/evidence.json"],
        )
        item = episode_sram.pending()[0]
    finally:
        episode_sram.reset()

    assert receipt.validation_status == "held"
    env, kwargs = item.envelope, item.write_options
    assert env.coord == [4, 4, 0]
    assert env.type == "emergent_pressure_resolution"
    assert env.payload["enrichment"]["supersedes_pressure_id"] == "p1"
    assert env.payload["enrichment"]["evidence_refs"] == list(_attempt_identity().evidence_refs)
    assert env.payload["enrichment"]["provenance_refs"] == ["memory/evidence.json"]
    assert kwargs == {"propose": False, "bypass_guardian": True, "skip_embed": False}


def test_episode_close_persists_attempt_then_resolves_then_stages(monkeypatch):
    calls = []
    persisted = {}
    episode = SimpleNamespace(
        episode_id="ep1",
        global_={"omega_packet": _packet().to_dict()},
        meta={"motor_program": _program_dict()},
        auto={},
    )
    result = OmegaEpisodeResult(
        episode_id="ep1",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params={},
        response_text=None,
        synthesis="method failed",
        reflection=None,
        episode_assessment={"completed": False, "quality": 0.1, "reason": "failed"},
        pressure_resolution={"pressure_id": "p1", "status": "supersede", "reason": "failed"},
        emergent_pressure=_pressure_dict(),
    )
    evidence = ExecutionEvidence.from_dict(
        {"program_id": "mp1", "episode_id": "ep1", "status": "failure"}
    )
    monkeypatch.setattr("auto_loop.motor_adapter.execution_evidence_from_fold", lambda ep: evidence)

    def persist(*args, **kwargs):
        calls.append("attempt")
        persisted["assessment"] = args[2]
        return (
            SimpleNamespace(path="memory/evidence.json"),
            SimpleNamespace(path="memory/assessment.json"),
        )

    monkeypatch.setattr("memory.motor_experience.persist_attempt_sequence", persist)
    monkeypatch.setattr(
        "pressure_loop.emergent_pressure.resolve_emergent_pressure",
        lambda *args, **kwargs: (
            calls.append("resolve") or SimpleNamespace(validation_status="committed")
        ),
    )
    monkeypatch.setattr(
        "pressure_loop.emergent_pressure.stage_emergent_pressure",
        lambda *args, **kwargs: calls.append("stage"),
    )
    for name in (
        "_apply_build_report_decision",
        "_record_trajectory_influence",
        "_record_signal_omega",
    ):
        monkeypatch.setattr(chain, name, lambda *args: None)

    chain._apply_omega_episode_result(episode, result)

    assert calls == ["attempt", "resolve", "stage"]
    assert persisted["assessment"]["program_fingerprint"]
    assert persisted["assessment"]["material_fingerprint"]
    assert persisted["assessment"]["material_signature"]
