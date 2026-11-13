from __future__ import annotations

from types import SimpleNamespace

import pytest

import cognition_chain as chain
from auto_loop.intake.run import auto_input_from_episode
from auto_loop.motor_adapter import execution_evidence_from_fold
from omega_loop.episode import packet as packet_module
from omega_loop.episode.run import _packet_from_episode


def _motor_program(*, episode_id: str = "ep1") -> dict:
    return {
        "program_id": f"mp_{episode_id}_01",
        "parent_program_id": "",
        "outcome_id": "out1",
        "episode_id": episode_id,
        "attempt_number": 1,
        "created_at": "2026-07-25T12:00:00Z",
        "outcome_contract": {
            "objective": "build the requested widget and prove it works",
            "acceptance_criteria": ["focused tests pass"],
            "constraints": ["preserve the requested behavior"],
            "priorities": ["correctness"],
        },
        "pathway": {
            "pathway_id": "derive-implement-verify",
            "revision": 1,
            "name": "derive implement verify",
            "method": "write tests, implement, and verify",
            "operational_rationale": "the request requires executable proof",
            "failed_predecessor_id": "",
            "change_evidence": [],
        },
        "execution_plan": {
            "capability_class": "development",
            "required_capabilities": [],
            "operations": ["implement the widget", "run focused tests"],
            "permitted_local_discretion": ["choose focused test flags"],
            "prohibited_deviations": ["change the requested outcome"],
            "return_to_mind_conditions": ["tests fail", "work is complete"],
        },
        "evidence_contract": {
            "required_artifacts": ["src/widget.py"],
            "required_source_proof": [],
            "required_checks": ["pytest tests/test_widget.py"],
            "completion_signals": ["focused tests pass"],
            "disqualifying_failures": ["fabricated evidence"],
        },
    }


def _episode() -> SimpleNamespace:
    program = _motor_program()
    skill_pack = {
        "type": "skill_execution_pack",
        "schema_version": "omega.skill_execution_pack.v1",
        "episode_id": "ep1",
        "dispatch_id": "dispatch-1",
        "action": "USE_SKILL",
        "objective": program["outcome_contract"]["objective"],
        "selected_skill": {
            "skill_name": "implementation",
            "skill_id": "skill-1",
            "skill_class": "development",
            "execution_kind": "APPLY",
            "source_path": "skills/implementation/SKILL.md",
        },
        "allowed_tools": ["patch_file", "run_command"],
        "forbidden_tools": [],
        "safety": {"safety_class": "command_safe"},
        "limits": {"max_files_changed": 5},
        "evidence_required": ["src/widget.py", "pytest tests/test_widget.py"],
        "completion_criteria": ["focused tests pass"],
        "expected_output_schema": {},
        "source_action_goal": {
            "episode_id": "ep1",
            "intent": "APPLY",
            "opened_by": "operator_command",
        },
    }
    execution_pack = {
        "episode_id": "ep1",
        "action": "EXECUTE_MOTOR_PROGRAM",
        "source_action_goal": {
            "episode_id": "ep1",
            "intent": "APPLY",
            "opened_by": "operator_command",
        },
        "execution_inputs": {
            "objective": program["outcome_contract"]["objective"],
            "motor_transport": "USE_SKILL",
            "motor_program": program,
            "skill_execution_pack": skill_pack,
        },
    }
    result = {
        "type": "qwen_skill_execution_result",
        "schema_version": "omega.qwen_skill_execution_result.v1",
        "episode_id": "ep1",
        "dispatch_id": "dispatch-1",
        "skill_name": "implementation",
        "skill_id": "skill-1",
        "execution_kind": "APPLY",
        "status": "success",
        "summary": "implemented the widget and ran its focused test",
        "tool_calls": [
            {
                "tool_name": "patch_file",
                "args": {"path": "src/widget.py"},
                "status": "success",
            },
            {
                "tool_name": "run_command",
                "args": {"command": "pytest tests/test_widget.py"},
                "status": "success",
            },
        ],
        "files_changed": ["src/widget.py"],
        "tests_run": ["pytest tests/test_widget.py"],
        "unresolved_blockers": [],
        "raw_bridge_result": {
            "ledger": {"touched_files": [{"path": "src/widget.py", "tool": "patch_file"}]}
        },
    }
    return SimpleNamespace(
        episode_id="ep1",
        opened_by="operator_command",
        pressure={
            "opened_by": "operator_command",
            "contributors": [
                {
                    "type": "operator_command",
                    "metadata": {"message": "Build me a tested widget"},
                }
            ],
        },
        meta={
            "execution_action_pack": execution_pack,
            "motor_program": program,
        },
        auto={
            "episode_id": "ep1",
            "execution_pack": execution_pack,
            "status": "success",
            "outputs": {"skill_result": result, "skill_status": "success"},
        },
        global_={
            "omega_packet": {
                "episode_id": "ep1",
                "intent": "APPLY",
                "action": "EXECUTE_MOTOR_PROGRAM",
                "tool": None,
                "domain": "software_engineering",
                "operator_text": "Build me a tested widget",
                "outcome": "success",
                "failure_reason": None,
                "raw_tool_result": None,
                "recall": [],
                "recall_error": None,
                "prior_mastery": None,
                "motor_program": program,
            },
            "skill_outcome": {
                "episode_id": "ep1",
                "dispatch_id": "dispatch-1",
                "intent_satisfied": True,
                "objective_complete": True,
                "more_work_needed": False,
                "unresolved_blockers": [],
                "continuation_signals": [],
                "continuation_pressure": None,
                "close_episode": True,
                "ask_operator": False,
                "summary": "implemented the widget and ran its focused test",
            },
        },
    )


def test_refresh_exposes_one_lineage_matched_factual_foldback_to_omega() -> None:
    """Catches rebuilding Omega's packet before agent evidence/verdict exists."""
    refresh = getattr(packet_module, "refresh_omega_execution_foldback", None)
    assert refresh is not None, "the post-agent foldback refresh contract is missing"

    episode = _episode()
    contract = refresh(episode, request_text="Build me a tested widget")
    rebuilt = _packet_from_episode(episode)

    assert contract["episode_id"] == "ep1"
    assert contract["request"] == {
        "origin": "operator_command",
        "text": "Build me a tested widget",
        "objective": "build the requested widget and prove it works",
    }
    assert contract["motor"] == {
        "program_id": "mp_ep1_01",
        "outcome_id": "out1",
        "acceptance_criteria": ["focused tests pass"],
    }
    assert contract["execution_evidence"]["files_changed"] == ["src/widget.py"]
    assert contract["execution_evidence"]["tests_run"] == ["pytest tests/test_widget.py"]
    assert contract["verdict"]["satisfied"] is True
    assert rebuilt is not None
    assert rebuilt.execution_foldback == contract


def test_auto_intake_rejects_motor_program_from_another_episode() -> None:
    episode = _episode()
    stale = _motor_program(episode_id="stale-episode")
    episode.meta["motor_program"] = stale
    episode.meta["execution_action_pack"]["execution_inputs"]["motor_program"] = stale

    with pytest.raises(ValueError, match="motor program episode_id"):
        auto_input_from_episode(episode)


@pytest.mark.parametrize("field_path", ["episode_id", "source_action_goal"])
def test_auto_intake_rejects_nested_skill_pack_from_another_episode(
    field_path: str,
) -> None:
    episode = _episode()
    skill_pack = episode.meta["execution_action_pack"]["execution_inputs"]["skill_execution_pack"]
    if field_path == "episode_id":
        skill_pack["episode_id"] = "stale-episode"
    else:
        skill_pack["source_action_goal"]["episode_id"] = "stale-episode"

    with pytest.raises(ValueError, match="skill execution pack episode identity"):
        auto_input_from_episode(episode)


def test_factual_evidence_rejects_motor_program_from_another_episode() -> None:
    episode = _episode()
    episode.meta["motor_program"] = _motor_program(episode_id="stale-episode")

    with pytest.raises(ValueError, match="motor program episode_id"):
        execution_evidence_from_fold(episode)


@pytest.mark.parametrize(
    ("field", "stale_value"),
    [
        ("episode_id", "stale-episode"),
        ("dispatch_id", "stale-dispatch"),
        ("skill_id", "stale-skill"),
    ],
)
def test_skill_foldback_rejects_result_from_another_dispatch(
    field: str,
    stale_value: str,
) -> None:
    episode = _episode()
    result = episode.auto["outputs"]["skill_result"]
    result[field] = stale_value

    validate = getattr(chain, "_validate_skill_foldback_lineage", None)
    assert validate is not None, "skill foldback lineage validator is missing"
    with pytest.raises(ValueError, match="skill foldback result"):
        validate(
            episode,
            episode.meta["execution_action_pack"],
            result,
        )


def test_resumed_foldback_preserves_the_initial_request_origin(monkeypatch) -> None:
    episode = _episode()
    episode.opened_by = "execution_gap"
    episode.pressure["opened_by"] = "execution_gap"
    episode.meta["execution_action_pack"]["source_action_goal"]["opened_by"] = "execution_gap"
    objective = _motor_program()["outcome_contract"]["objective"]
    report = SimpleNamespace(
        build_task=objective,
        origin_opened_by="operator_command",
        original_request="Build me a tested widget",
    )
    monkeypatch.setattr(
        "memory.execution_report.current_execution_report",
        lambda: report,
    )

    contract = packet_module.refresh_omega_execution_foldback(
        episode,
        request_text="Build me a tested widget",
    )

    assert contract is not None
    assert contract["request"]["origin"] == "operator_command"


def test_captured_request_survives_report_bucket_change(monkeypatch) -> None:
    episode = _episode()
    episode.opened_by = "execution_gap"
    objective = _motor_program()["outcome_contract"]["objective"]
    report = SimpleNamespace(
        build_task=objective,
        origin_opened_by="operator_command",
        original_request="Build me a tested widget",
    )
    current = {"report": report}
    monkeypatch.setattr(
        "memory.execution_report.current_execution_report",
        lambda: current["report"],
    )

    capture = getattr(packet_module, "capture_execution_request_context", None)
    assert capture is not None, "pre-completion request capture is missing"
    request = capture(episode)
    current["report"] = None

    contract = packet_module.refresh_omega_execution_foldback(
        episode,
        request_text=request["text"],
        request_origin=request["origin"],
    )

    assert contract is not None
    assert contract["request"] == {
        "origin": "operator_command",
        "text": "Build me a tested widget",
        "objective": objective,
    }


def test_execution_foldback_packet_keeps_plugin_provenance(monkeypatch) -> None:
    episode = _episode()
    episode.auto["outputs"].update(
        {
            "plugin_result": {
                "plugin_id": "plugin.synthetic",
                "app_id": "app.synthetic",
                "capability": "lookup",
                "status": "completed",
                "data": {"value": "observed"},
            },
            "plugin_plan": {
                "plan_id": "plan-1",
                "authority_digest": "a" * 64,
            },
        }
    )
    monkeypatch.setattr("omega_loop.episode.packet.load_mastery", lambda: {})

    rebuilt = _packet_from_episode(episode)

    assert rebuilt.foldback_packet["plugin_execution"]["plan_id"] == "plan-1"
    assert rebuilt.raw_tool_result["plugin_id"] == "plugin.synthetic"
