from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from core.paths import OMEGA_ROOT
from global_loop.intent.run import _map_opened_by_to_intent, run as run_intent
from meta_loop.action.run import META_ACTIONS, run as run_action
from meta_loop.intake.run import MetaAInput
from meta_loop.loop import run_meta_a
from meta_loop.tool.run import run as run_tool
from pressure_loop.contributors import (
    ALLOWED_INTENTS,
    EpisodeIgnition,
    make_contributor,
)


def _pressure(capabilities: list[str]) -> dict:
    payload = {
        "pressure_id": "p1",
        "name": "derivation_gap",
        "source_episode_id": "ep0",
        "outcome_id": "out1",
        "desired_state": "verified understanding",
        "observed_state": "retrieval only",
        "gap_description": "no demonstration",
        "evidence": ["knowledge delta 0"],
        "failed_program_ids": ["mp0"],
        "urgency": 0.9,
        "persistence": 2,
        "proposed_action": {
            "intended_transformation": "produce a demonstration",
            "candidate_pathway": "derive-implement-verify",
            "required_capabilities": capabilities,
        },
        "activation_conditions": ["gap unresolved"],
        "resolution_conditions": ["demonstration verified"],
        "supersedes_pressure_id": "",
    }
    return {"emergent_pressure": payload, "message": "close the derivation gap"}


def _meta_input(capabilities: list[str]) -> MetaAInput:
    return MetaAInput(
        episode_id="ep1",
        action_goal={
            "episode_id": "ep1",
            "intent": "PURSUE_OUTCOME",
            "confidence": 0.9,
            "reason": "test",
        },
        pressure={
            "aggregate_pressure": 0.9,
            "contributors": [{"id": "derivation_gap", "detail": _pressure(capabilities)}],
        },
        trajectory_snapshot={},
    )


def _ignition(*, source: str, metadata: dict, opened_by: str = "derivation_gap"):
    dominant = make_contributor(source, opened_by, 0.9, metadata)
    return EpisodeIgnition(
        episode_id="ep1",
        aggregate_pressure=0.9,
        opened_by=opened_by,
        dominant_contributor=dominant,
        contributors=(dominant,),
        trajectory_snapshot=None,
    )


def test_omega_emergent_pressure_routes_to_stable_intent():
    assert (
        _map_opened_by_to_intent(
            "derivation_gap",
            "",
            source="omega",
            metadata=_pressure(["web_research"]),
        )
        == "PURSUE_OUTCOME"
    )


def test_unknown_non_omega_pressure_still_falls_back_safely():
    assert _map_opened_by_to_intent("untrusted_unknown", "", source="system", metadata={}) == "IDLE"


def test_non_omega_directive_cannot_emit_pursue_outcome():
    metadata = _pressure(["testing"])
    metadata["directive_intent"] = "PURSUE_OUTCOME"
    ignition = _ignition(source="system", metadata=metadata, opened_by="untrusted_unknown")
    assert run_intent(ignition) == "IDLE"


@pytest.mark.parametrize("malformed", [None, "not-a-pressure"])
def test_omega_non_dict_emergent_pressure_fails_hard_at_run_boundary(malformed):
    ignition = _ignition(source="omega", metadata={"emergent_pressure": malformed})
    with pytest.raises(ValueError, match="emergent_pressure must be a dictionary"):
        run_intent(ignition)


def test_malformed_omega_emergent_pressure_fails_hard():
    malformed = _pressure(["web_research"])
    malformed["emergent_pressure"]["pressure_id"] = ""
    with pytest.raises(ValueError, match="identity fields"):
        _map_opened_by_to_intent("derivation_gap", "", source="omega", metadata=malformed)


def test_pursue_outcome_selects_skill_transport_for_code_capabilities():
    expected = deepcopy(_pressure(["code_execution", "testing"])["emergent_pressure"])
    state = run_action(_meta_input(["code_execution", "testing"]))
    assert state.action == "EXECUTE_MOTOR_PROGRAM"
    assert state.execution_inputs["motor_transport"] == "USE_SKILL"
    assert state.execution_inputs["emergent_pressure"] == expected
    assert state.execution_inputs["objective"] == "verified understanding"


def test_pursue_outcome_selects_tool_transport_for_research_capabilities():
    expected = deepcopy(_pressure(["web_research"])["emergent_pressure"])
    state = run_action(_meta_input(["web_research"]))
    assert state.action == "EXECUTE_MOTOR_PROGRAM"
    assert state.execution_inputs["motor_transport"] == "USE_TOOL"
    state = run_tool(state)
    assert state.tool == "web_fetch"
    assert state.execution_inputs["emergent_pressure"] == expected


def test_skill_motor_transport_selects_no_raw_tool():
    state = run_tool(run_action(_meta_input(["building"])))
    assert state.tool is None
    assert state.execution_inputs["motor_transport"] == "USE_SKILL"


def test_skill_motor_transport_attaches_skill_execution_pack(monkeypatch):
    calls: list[dict] = []

    def _attach(episode, pack):
        calls.append(pack)
        return {"selected_skill": {"skill_id": "build", "skill_name": "Build"}}

    monkeypatch.setattr("meta_loop.skill.run.attach_skill_execution_pack", _attach)
    detail = _pressure(["testing"])
    expected = deepcopy(detail["emergent_pressure"])
    episode = SimpleNamespace(
        episode_id="ep1",
        global_={
            "intent_decision": {
                "episode_id": "ep1",
                "intent": "PURSUE_OUTCOME",
                "confidence": 0.9,
                "reason": "test",
            }
        },
        pressure={
            "opened_by": "derivation_gap",
            "aggregate_pressure": 0.9,
            "contributors": [{"id": "derivation_gap", "detail": detail}],
        },
        meta={},
    )
    pack = run_meta_a(episode)
    assert pack["action"] == "EXECUTE_MOTOR_PROGRAM"
    assert pack["execution_inputs"]["emergent_pressure"] == expected
    assert (
        episode.meta["execution_action_pack"]["execution_inputs"]["emergent_pressure"] == expected
    )
    assert calls == [pack]


def test_motor_capabilities_select_a_write_capable_skill_pack():
    from meta_loop.skill.run import attach_skill_execution_pack
    from meta_loop.pack.run import execution_action_pack_to_dict, run as run_pack

    state = run_action(_meta_input(["building", "code_execution", "testing"]))
    pack = execution_action_pack_to_dict(run_pack(state))
    episode = SimpleNamespace(
        episode_id="ep1",
        opened_by="technical_depth_gap",
        pressure={},
        meta={},
    )

    attached = attach_skill_execution_pack(episode, pack)

    assert attached is not None
    assert attached["limits"]["max_files_changed"] > 0
    assert "write_file" in attached["allowed_tools"] or "patch_file" in attached["allowed_tools"]


def test_emergent_transport_vocabularies_are_exhaustive():
    global_cfg = yaml.safe_load(
        (OMEGA_ROOT / "config" / "global_intent.yaml").read_text(encoding="utf-8")
    )["global_intent"]
    meta_cfg = yaml.safe_load((OMEGA_ROOT / "config" / "meta.yaml").read_text(encoding="utf-8"))[
        "meta"
    ]
    global_schema = yaml.safe_load(
        (OMEGA_ROOT / "schema" / "global.schema.yaml").read_text(encoding="utf-8")
    )
    meta_schema = yaml.safe_load(
        (OMEGA_ROOT / "schema" / "meta.schema.yaml").read_text(encoding="utf-8")
    )
    assert "PURSUE_OUTCOME" in ALLOWED_INTENTS
    assert "PURSUE_OUTCOME" in global_cfg["allowed"]
    assert "PURSUE_OUTCOME" in global_schema["definitions"]["allowed_intent"]["enum"]
    assert "EXECUTE_MOTOR_PROGRAM" in META_ACTIONS
    assert "EXECUTE_MOTOR_PROGRAM" in meta_cfg["action"]["meta_actions"]
    action_enum = meta_schema["definitions"]["ExecutionActionPack"]["properties"]["action"]["enum"]
    assert "EXECUTE_MOTOR_PROGRAM" in action_enum
