from types import SimpleNamespace

import pytest

from auto_loop._auto_state import AutoInput, MemoryEnvelope
from auto_loop.execute.run import execute
from auto_loop.fold.run import _fold_episode_auto, auto_outcome_from_fold
from auto_loop.motor_adapter import (
    effective_motor_action,
    execution_evidence_from_fold,
    motor_program_from_pack,
)
from auto_loop.promptpack.run import build_prompt_pack
from auto_loop.route.run import tool_call_is_direct
from schema.motor_program import MotorProgram
from schema.qwen_skill_dispatch import (
    QwenSkillExecutionDispatch,
    build_directive,
    build_qwen_skill_dispatch,
    motor_authority,
)
from schema.qwen_skill_result import normalize_qwen_result
from schema.skill_execution_pack import SkillExecutionPack


def _motor_program_dict() -> dict:
    return {
        "program_id": "mp1",
        "parent_program_id": "",
        "outcome_id": "out1",
        "episode_id": "ep1",
        "attempt_number": 1,
        "created_at": "2026-07-11T12:00:00Z",
        "outcome_contract": {
            "objective": "implement and verify the requested behavior",
            "acceptance_criteria": ["pytest passes"],
            "constraints": ["do not change the objective"],
            "priorities": ["correctness"],
        },
        "pathway": {
            "pathway_id": "derive-implement-verify",
            "revision": 1,
            "name": "derive implement verify",
            "method": "write tests, implement, and verify",
            "operational_rationale": "the result needs executable proof",
            "failed_predecessor_id": "",
            "change_evidence": [],
        },
        "execution_plan": {
            "capability_class": "development",
            "required_capabilities": [],
            "operations": ["run the focused pytest suite", "inspect the diff"],
            "permitted_local_discretion": ["choose test flags"],
            "prohibited_deviations": ["change the requested outcome"],
            "return_to_mind_conditions": ["tests fail", "work is complete"],
        },
        "evidence_contract": {
            "required_artifacts": ["files_changed"],
            "required_source_proof": [],
            "required_checks": ["tests_run"],
            "completion_signals": ["pytest passes"],
            "disqualifying_failures": ["fabricated evidence"],
        },
    }


def _execution_pack(*, action: str, motor_transport: str, query: str = "") -> dict:
    inputs = {
        "motor_transport": motor_transport,
        "motor_program": _motor_program_dict(),
    }
    if query:
        inputs["query"] = query
    return {
        "episode_id": "ep1",
        "action": action,
        "tool": "web_fetch",
        "execution_inputs": inputs,
    }


def _dispatch_with_motor_program() -> QwenSkillExecutionDispatch:
    return QwenSkillExecutionDispatch(
        episode_id="ep1",
        dispatch_id="dispatch1",
        skill_name="python-build",
        skill_id="python-build",
        skill_class="development",
        execution_kind="build",
        objective="implement and verify the requested behavior",
        skill_markdown="# Python build\nUse tests first.",
        allowed_tools=("read_file", "write_file", "shell"),
        forbidden_tools=("write_memory",),
        safety={},
        limits={"max_steps": 20, "max_duration_ms": 120000, "max_files_changed": 10},
        evidence_required=("files_changed", "tests_run"),
        completion_criteria=("pytest passes",),
        expected_output_schema={"status": "success|failure"},
        source_action_goal={"intent": "PURSUE_OUTCOME"},
        source_skill_execution_pack={},
        motor_program=_motor_program_dict(),
    )


def _episode_with_qwen_files_and_tests():
    return SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": _motor_program_dict()},
        auto={
            "outputs": {
                "qwen_skill_result": {
                    "status": "success",
                    "files_changed": ["invented.py"],
                    "commands_run": ["invented command"],
                    "tests_run": ["invented test"],
                    "artifacts": [],
                    "raw_bridge_result": {
                        "ledger": {
                            "touched_files": [{"path": "src/model.py", "tool": "write_file"}]
                        }
                    },
                    "tool_calls": [
                        {
                            "tool_call_id": "write-model",
                            "tool_name": "write_file",
                            "args": {"path": "src/model.py"},
                            "status": "success",
                        },
                        {
                            "tool_call_id": "test-model",
                            "tool_name": "execute_command",
                            "args": {"command": "pytest -q"},
                            "status": "success",
                            "result": {"exit_code": 0},
                        },
                    ],
                    "failure_class": None,
                    "intent": "IGNORE_AND_REPLAN",
                    "pressure": 999,
                    "pathway": {"pathway_id": "bridge-owned"},
                    "outcome": "bridge-owned",
                    "acceptance_criteria": ["bridge decides"],
                }
            }
        },
    )


def _empty_memory() -> MemoryEnvelope:
    return MemoryEnvelope(False, "skipped", None, (), (), None)


def test_motor_transport_resolves_without_rewriting_program():
    pack = _execution_pack(action="EXECUTE_MOTOR_PROGRAM", motor_transport="USE_SKILL")
    assert effective_motor_action(pack) == "USE_SKILL"
    assert pack["action"] == "EXECUTE_MOTOR_PROGRAM"


@pytest.mark.parametrize("legacy_evidence", [(), ("STALE_LEGACY_EVIDENCE",)])
def test_motor_skill_foldback_uses_canonical_program_evidence_contract(
    monkeypatch,
    legacy_evidence,
):
    import cognition_chain
    from memory.episode_sram import open_bucket, reset

    invoked = []
    episode = SimpleNamespace(
        episode_id="ep1",
        opened_by="technical_depth_gap",
        pressure={"opened_by": "technical_depth_gap"},
        global_={},
        meta={
            "motor_program": _motor_program_dict(),
            "execution_action_pack": _execution_pack(
                # AUTO resolves the motor transport before fold-back; authority
                # must survive this mutable action-label rewrite.
                action="USE_SKILL",
                motor_transport="USE_SKILL",
            ),
        },
        auto={
            "outputs": {
                "skill_result": {
                    "episode_id": "ep1",
                    "dispatch_id": "dispatch-1",
                    "skill_id": "skill-1",
                    "skill_name": "implementation",
                    "status": "success",
                    "summary": "executor returned",
                    "tool_calls": [],
                }
            }
        },
    )
    episode.meta["execution_action_pack"]["execution_inputs"]["skill_execution_pack"] = {
        "episode_id": "ep1",
        "dispatch_id": "dispatch-1",
        "selected_skill": {
            "skill_id": "skill-1",
            "skill_name": "implementation",
        },
        "evidence_required": list(legacy_evidence),
    }

    def folded(*args, **kwargs):
        invoked.append((args, kwargs))
        return {
            "meta_b": {"outcome_kind": "partial"},
            "global_b": {
                "objective_complete": False,
                "more_work_needed": False,
                "unresolved_blockers": [],
            },
        }

    monkeypatch.setattr("meta_loop.skill.foldback.fold_skill_result", folded)
    monkeypatch.setattr("meta_loop.skill.foldback.attach_skill_foldback", lambda *_: None)
    monkeypatch.setattr(
        "deepsleep_loop.skill_consolidation.consolidate_skill_episode",
        lambda *_args, **_kwargs: {},
    )

    open_bucket("ep1", pipeline_id=0)
    try:
        cognition_chain._run_skill_foldback(episode)

        assert len(invoked) == 1
        assert invoked[0][1]["objective"] == "implement and verify the requested behavior"
        assert invoked[0][1]["evidence_required"] == ("files_changed", "tests_run")
    finally:
        # Test-only reset: production cleanup refuses pending reports.
        reset()


def test_build_motor_authority_drops_plan_present_unless_plan_requested():
    program = _motor_program_dict()
    program["outcome_contract"]["objective"] = "build a basic todo app green"
    program["evidence_contract"]["required_checks"] = ["plan_present", "pytest"]

    objective, evidence_required, _, _ = motor_authority(program)

    assert objective == "build a basic todo app green"
    assert "pytest" in evidence_required
    assert "plan_present" not in evidence_required


def test_plan_requested_motor_authority_keeps_plan_present():
    program = _motor_program_dict()
    program["outcome_contract"]["objective"] = "write an implementation plan for a todo app"
    program["evidence_contract"]["required_checks"] = ["plan_present"]

    _, evidence_required, _, _ = motor_authority(program)

    assert "plan_present" in evidence_required


def test_literal_non_motor_skill_foldback_preserves_meta_a_evidence_contract(monkeypatch):
    import cognition_chain
    from memory import episode_sram

    invoked = []
    episode_sram.open_bucket("ep1", pipeline_id=0)
    episode = SimpleNamespace(
        episode_id="ep1",
        opened_by="operator_command",
        pressure={},
        global_={},
        meta={
            "execution_action_pack": {
                "episode_id": "ep1",
                "action": "USE_SKILL",
                "execution_inputs": {
                    "objective": "review the design",
                    "skill_execution_pack": {
                        "episode_id": "ep1",
                        "dispatch_id": "dispatch-1",
                        "selected_skill": {
                            "skill_id": "skill-1",
                            "skill_name": "review",
                        },
                        "evidence_required": ["findings", "summary"],
                    },
                },
            }
        },
        auto={
            "outputs": {
                "skill_result": {
                    "episode_id": "ep1",
                    "dispatch_id": "dispatch-1",
                    "skill_id": "skill-1",
                    "skill_name": "review",
                    "status": "success",
                    "summary": "executor returned",
                    "tool_calls": [],
                }
            }
        },
    )

    def folded(*args, **kwargs):
        invoked.append((args, kwargs))
        return {
            "meta_b": {"outcome_kind": "partial"},
            "global_b": {
                "objective_complete": False,
                "more_work_needed": False,
                "unresolved_blockers": [],
            },
        }

    monkeypatch.setattr("meta_loop.skill.foldback.fold_skill_result", folded)
    monkeypatch.setattr("meta_loop.skill.foldback.attach_skill_foldback", lambda *_: None)
    monkeypatch.setattr(
        "deepsleep_loop.skill_consolidation.consolidate_skill_episode",
        lambda *_args, **_kwargs: {},
    )

    try:
        cognition_chain._run_skill_foldback(episode)
    finally:
        # Test-only reset: production cleanup refuses pending reports.
        episode_sram.reset()

    assert invoked[0][1]["objective"] == "review the design"
    assert invoked[0][1]["evidence_required"] == ("findings", "summary")


def test_motor_program_from_pack_uses_canonical_contract():
    program = motor_program_from_pack(
        _execution_pack(action="EXECUTE_MOTOR_PROGRAM", motor_transport="USE_TOOL")
    )
    assert isinstance(program, MotorProgram)
    assert program.to_dict() == _motor_program_dict()


def test_tool_prompt_contains_program_and_uses_first_operation_as_query_fallback():
    pack = _execution_pack(action="EXECUTE_MOTOR_PROGRAM", motor_transport="USE_TOOL")
    prompt = build_prompt_pack(pack, _empty_memory())
    assert prompt.prompt_type == "TOOL_CALL_SYSTEM"
    assert prompt.tool_call_spec["params"]["query"] == "run the focused pytest suite"
    assert (
        '"method": "write tests, implement, and verify"' in prompt.context_blocks["motor_program"]
    )
    assert pack["execution_inputs"].get("query") is None


def test_explicit_tool_query_remains_authoritative_and_routes_directly():
    pack = _execution_pack(
        action="EXECUTE_MOTOR_PROGRAM",
        motor_transport="USE_TOOL",
        query="operator supplied query",
    )
    prompt = build_prompt_pack(pack, _empty_memory())
    assert prompt.tool_call_spec["params"]["query"] == "operator supplied query"
    assert tool_call_is_direct(pack) is True


def test_tool_executor_uses_effective_action_and_records_it_without_mutating_pack():
    pack = _execution_pack(action="EXECUTE_MOTOR_PROGRAM", motor_transport="USE_TOOL")
    prompt = build_prompt_pack(pack, _empty_memory())
    calls = []

    def invoke(tool_name, args):
        calls.append((tool_name, args))
        return {"ok": True, "result": {"url": "https://example.test"}, "elapsed_ms": 2}

    result = execute(
        prompt,
        None,
        pack,
        _empty_memory(),
        "ep1",
        runtime_client=object(),
        tool_invoker=invoke,
    )
    assert calls[0][1]["query"] == "run the focused pytest suite"
    assert result["outputs"]["effective_action"] == "USE_TOOL"
    assert pack["action"] == "EXECUTE_MOTOR_PROGRAM"


def test_fold_records_effective_action_but_preserves_original_action():
    pack = _execution_pack(action="EXECUTE_MOTOR_PROGRAM", motor_transport="USE_TOOL")
    ai = AutoInput("ep1", {}, {}, pack, {}, _motor_program_dict())
    prompt = build_prompt_pack(pack, _empty_memory())
    folded = _fold_episode_auto(
        ai,
        status="success",
        cycle_kind="success",
        memory_envelope=_empty_memory(),
        model_profile=None,
        model_id=None,
        prompt_pack=prompt,
        exec_result={"outputs": {"effective_action": "USE_TOOL"}},
        runtime_ms=2,
    )
    assert folded["action"] == "EXECUTE_MOTOR_PROGRAM"
    assert folded["effective_action"] == "USE_TOOL"
    assert folded["execution_pack"]["action"] == "EXECUTE_MOTOR_PROGRAM"


@pytest.mark.parametrize("action", ["USE_TOOL", "USE_SKILL", "IDLE"])
def test_legacy_fold_shape_has_no_effective_action_fields(action):
    pack = {"action": action, "execution_inputs": {}}
    ai = AutoInput("ep1", {}, {}, pack, {}, None)
    prompt = build_prompt_pack(pack, _empty_memory())
    folded = _fold_episode_auto(
        ai,
        status="idle" if action == "IDLE" else "success",
        cycle_kind="idle" if action == "IDLE" else "success",
        memory_envelope=_empty_memory(),
        model_profile=None,
        model_id=None,
        prompt_pack=prompt,
        exec_result={"outputs": {}},
        runtime_ms=2,
    )
    assert "effective_action" not in folded
    assert "effective_action" not in folded["outputs"]
    assert "effective_action" not in folded["telemetry"]
    outcome = auto_outcome_from_fold(ai, folded)
    assert "effective_action" not in outcome.auto["outputs"]
    assert "effective_action" not in outcome.auto["telemetry"]


def test_qwen_directive_places_exact_omega_motor_program_before_skill_body():
    dispatch = _dispatch_with_motor_program()
    directive = build_directive(dispatch)
    motor_at = directive.index("## Omega motor program")
    skill_at = directive.index("## Skill (SKILL.md body)")
    assert motor_at < skill_at
    assert (
        "You may not change the objective, acceptance criteria, pathway, or evidence contract"
        in directive
    )
    assert dispatch.to_dict()["motor_program"] == _motor_program_dict()


def test_motor_directive_uses_only_canonical_program_authority():
    values = dict(_dispatch_with_motor_program().__dict__)
    values.update(
        objective="STALE LEGACY OBJECTIVE",
        evidence_required=("STALE_LEGACY_EVIDENCE",),
        completion_criteria=("STALE LEGACY COMPLETION",),
    )
    directive = build_directive(QwenSkillExecutionDispatch(**values))
    assert "STALE LEGACY OBJECTIVE" not in directive
    assert "STALE_LEGACY_EVIDENCE" not in directive
    assert "STALE LEGACY COMPLETION" not in directive
    assert "implement and verify the requested behavior" in directive
    assert "pytest passes" in directive
    assert '"required_checks": ["tests_run"]' in directive


def test_motor_qwen_dispatch_payload_replaces_nested_legacy_authority():
    stale_authority = {
        "objective": "STALE LEGACY OBJECTIVE",
        "desired_outcome": "STALE LEGACY OUTCOME",
        "acceptance_criteria": ["STALE LEGACY COMPLETION"],
        "constraints": ["STALE LEGACY CONSTRAINT"],
        "priorities": ["STALE LEGACY PRIORITY"],
        "pathway": {"method": "STALE LEGACY PATHWAY"},
        "evidence_contract": {"required_checks": ["STALE LEGACY EVIDENCE CONTRACT"]},
    }
    stale_pack = SkillExecutionPack(
        episode_id="ep1",
        dispatch_id="dispatch1",
        objective="STALE LEGACY OBJECTIVE",
        selected_skill={
            "skill_name": "python-build",
            "skill_id": "python-build",
            "skill_class": "development",
            "execution_kind": "build",
            "source_path": "/skills/python-build/SKILL.md",
        },
        allowed_tools=("shell",),
        forbidden_tools=(),
        safety={},
        limits={},
        evidence_required=("STALE_LEGACY_EVIDENCE",),
        completion_criteria=("STALE LEGACY COMPLETION",),
        expected_output_schema={"status": "success|failure"},
        source_action_goal=stale_authority,
    )
    skill = SimpleNamespace(body_markdown="# Skill")
    dispatch = build_qwen_skill_dispatch(
        stale_pack,
        skill,
        motor_program=_motor_program_dict(),
    ).to_dict()
    assert dispatch["objective"] == "implement and verify the requested behavior"
    assert dispatch["completion_criteria"] == ["pytest passes"]
    assert dispatch["evidence_required"] == ["files_changed", "tests_run"]
    assert dispatch["source_skill_execution_pack"]["objective"] == dispatch["objective"]
    assert dispatch["source_skill_execution_pack"]["completion_criteria"] == ["pytest passes"]
    assert dispatch["source_skill_execution_pack"]["evidence_required"] == [
        "files_changed",
        "tests_run",
    ]
    authority_keys = set(stale_authority)
    assert authority_keys.isdisjoint(dispatch["source_action_goal"])
    assert authority_keys.isdisjoint(dispatch["source_skill_execution_pack"]["source_action_goal"])

    def assert_no_stale_values(value):
        if isinstance(value, dict):
            for nested in value.values():
                assert_no_stale_values(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                assert_no_stale_values(nested)
        elif isinstance(value, str):
            assert "STALE LEGACY" not in value

    assert_no_stale_values(dispatch["source_action_goal"])
    assert_no_stale_values(dispatch["source_skill_execution_pack"])


def test_execution_evidence_preserves_only_real_qwen_outputs():
    evidence = execution_evidence_from_fold(_episode_with_qwen_files_and_tests())
    assert evidence.program_id == "mp1"
    assert evidence.files_changed == ("src/model.py",)
    assert evidence.commands_run == ("pytest -q",)
    assert evidence.tests_run == ("pytest -q",)
    assert evidence.status == "success"
    evidence_dict = evidence.to_dict()
    for authority_key in ("intent", "pressure", "pathway", "outcome", "acceptance_criteria"):
        assert authority_key not in evidence_dict


@pytest.mark.parametrize("status", ["partial", "yielded"])
def test_execution_evidence_preserves_nonterminal_qwen_status(status):
    episode = _episode_with_qwen_files_and_tests()
    episode.auto["outputs"]["qwen_skill_result"]["status"] = status
    evidence = execution_evidence_from_fold(episode)
    assert evidence.status == status
    assert evidence.files_changed == ("src/model.py",)
    assert evidence.tests_run == ("pytest -q",)


def _evidence_admitted(program_dict, evidence):
    from pressure_loop.episode_journal import _evidence_contract_admitted

    return _evidence_contract_admitted(MotorProgram.from_dict(program_dict), evidence)


def _qwen_episode(raw: dict):
    normalized = normalize_qwen_result(
        raw,
        dispatch=_dispatch_with_motor_program(),
    ).to_dict()
    return SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": _motor_program_dict()},
        auto={"outputs": {"qwen_skill_result": normalized}},
    )


def test_real_successful_qwen_test_call_verifies_from_executor_provenance():
    episode = _qwen_episode(
        {
            "status": "success",
            "ledger": {"touched_files": [{"path": "src/model.py", "tool": "write_file"}]},
            "tool_calls": [
                {
                    "tool_call_id": "call-test-1",
                    "tool_name": "execute_command",
                    "args": {"command": "python -m pytest -q"},
                    "status": "success",
                    "stdout": "2 passed",
                    "result": {"exit_code": 0},
                }
            ],
        }
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results[0]["status"] == "passed"
    assert evidence.verification_results[0]["requested"] == "tests_run"
    assert evidence.verification_results[0]["executor_provenance"]["tool_call_id"] == "call-test-1"
    assert _evidence_admitted(_motor_program_dict(), evidence)


def test_successful_tool_call_id_containing_429_is_not_rate_limited():
    episode = _qwen_episode(
        {
            "status": "success",
            "ledger": {"touched_files": [{"path": "src/model.py", "tool": "write_file"}]},
            "tool_calls": [
                {
                    "tool_call_id": "chatcmpl-tool-a429b",
                    "tool_name": "execute_command",
                    "args": {"command": "python -m pytest -q"},
                    "status": "success",
                    "stdout": "2 passed",
                    "result": {"exit_code": 0},
                }
            ],
        }
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.failure_kind == ""
    assert _evidence_admitted(_motor_program_dict(), evidence)


def test_filesystem_checks_match_successful_test_path_receipt():
    program = _motor_program_dict()
    program["evidence_contract"]["required_checks"] = [
        "directory creation check",
        "file existence check",
    ]
    normalized = normalize_qwen_result(
        {
            "status": "success",
            "ledger": {"touched_files": [{"path": "src/model.py", "tool": "write_file"}]},
            "tool_calls": [
                {
                    "tool_call_id": "check-files",
                    "tool_name": "execute_command",
                    "args": {"command": "Test-Path src; Test-Path src/model.py"},
                    "status": "success",
                    "stdout": "True\nTrue",
                    "result": {"exit_code": 0},
                }
            ],
        },
        dispatch=_dispatch_with_motor_program(),
    ).to_dict()
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": program},
        auto={"outputs": {"qwen_skill_result": normalized}},
    )

    evidence = execution_evidence_from_fold(episode)

    assert [row["status"] for row in evidence.verification_results] == [
        "passed",
        "passed",
    ]
    assert _evidence_admitted(program, evidence)


def test_filesystem_check_rejects_false_test_path_output():
    program = _motor_program_dict()
    program["evidence_contract"]["required_checks"] = [
        "file existence check: src/model.py",
    ]
    normalized = normalize_qwen_result(
        {
            "status": "success",
            "tool_calls": [
                {
                    "tool_call_id": "check-missing-file",
                    "tool_name": "execute_command",
                    "args": {"command": "Test-Path src/model.py"},
                    "status": "success",
                    "stdout": "False",
                    "result": {"exit_code": 0},
                }
            ],
        },
        dispatch=_dispatch_with_motor_program(),
    ).to_dict()
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": program},
        auto={"outputs": {"qwen_skill_result": normalized}},
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results[0]["status"] == "failed"
    assert not _evidence_admitted(program, evidence)


@pytest.mark.parametrize(
    ("stdout", "expected_status"),
    [("True", "passed"), ("False", "failed")],
)
def test_exact_test_path_requirement_uses_matching_executor_receipt(
    stdout: str,
    expected_status: str,
):
    program = _motor_program_dict()
    program["evidence_contract"]["required_checks"] = ["Test-Path src/model.py"]
    normalized = normalize_qwen_result(
        {
            "status": "success",
            "tool_calls": [
                {
                    "tool_call_id": "exact-test-path",
                    "tool_name": "execute_command",
                    "args": {"command": "Test-Path src/model.py"},
                    "status": "success",
                    "stdout": stdout,
                    "result": {"exit_code": 0},
                }
            ],
        },
        dispatch=_dispatch_with_motor_program(),
    ).to_dict()
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": program},
        auto={"outputs": {"qwen_skill_result": normalized}},
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results[0]["status"] == expected_status
    assert (
        evidence.verification_results[0]["executor_provenance"]["tool_call_id"] == "exact-test-path"
    )


def test_scoped_pytest_requirements_bind_to_their_matching_final_calls():
    program = _motor_program_dict()
    program["evidence_contract"]["required_checks"] = [
        "pytest tests/unit",
        "pytest tests/integration",
    ]
    normalized = normalize_qwen_result(
        {
            "status": "success",
            "ledger": {"touched_files": [{"path": "src/model.py", "tool": "write_file"}]},
            "tool_calls": [
                {
                    "tool_call_id": "unit-red",
                    "tool_name": "execute_command",
                    "args": {"command": "python -m pytest tests/unit -q"},
                    "status": "failure",
                    "stdout": "1 failed",
                    "result": {"exit_code": 1},
                },
                {
                    "tool_call_id": "integration-green",
                    "tool_name": "execute_command",
                    "args": {"command": "python -m pytest tests/integration -q"},
                    "status": "success",
                    "stdout": "3 passed",
                    "result": {"exit_code": 0},
                },
            ],
        },
        dispatch=_dispatch_with_motor_program(),
    ).to_dict()
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": program},
        auto={"outputs": {"qwen_skill_result": normalized}},
    )

    evidence = execution_evidence_from_fold(episode)

    assert [row["status"] for row in evidence.verification_results] == [
        "failed",
        "passed",
    ]
    assert [
        row["executor_provenance"]["tool_call_id"] for row in evidence.verification_results
    ] == ["unit-red", "integration-green"]
    assert not _evidence_admitted(program, evidence)


def test_scoped_pytest_filename_containing_test_path_is_not_a_filesystem_check():
    program = _motor_program_dict()
    program["evidence_contract"]["required_checks"] = [
        "pytest tests/test_path.py",
    ]
    normalized = normalize_qwen_result(
        {
            "status": "success",
            "tool_calls": [
                {
                    "tool_call_id": "pytest-test-path-file",
                    "tool_name": "execute_command",
                    "args": {"command": "python -m pytest tests/test_path.py -q"},
                    "status": "success",
                    "stdout": "2 passed",
                    "result": {"exit_code": 0},
                }
            ],
        },
        dispatch=_dispatch_with_motor_program(),
    ).to_dict()
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": program},
        auto={"outputs": {"qwen_skill_result": normalized}},
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results[0]["status"] == "passed"
    assert (
        evidence.verification_results[0]["executor_provenance"]["tool_call_id"]
        == "pytest-test-path-file"
    )


def test_failed_qwen_test_call_fails_despite_bridge_pass_claims():
    episode = _qwen_episode(
        {
            "status": "success",
            "files_changed": ["src/model.py"],
            "tests_run": ["pytest -q"],
            "evidence": {"verification_results": [{"status": "passed", "requested": "tests_run"}]},
            "tool_calls": [
                {
                    "tool_call_id": "call-test-red",
                    "tool_name": "execute_command",
                    "args": {"command": "pytest -q"},
                    "status": "failure",
                    "stdout": "1 failed",
                    "result": {"exit_code": 1},
                }
            ],
        }
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results[0]["status"] == "failed"
    assert not _evidence_admitted(_motor_program_dict(), evidence)


def test_tdd_red_then_green_uses_latest_executor_check_state():
    episode = _qwen_episode(
        {
            "status": "success",
            "ledger": {"touched_files": [{"path": "src/model.py", "tool": "write_file"}]},
            "tool_calls": [
                {
                    "tool_call_id": "call-test-red",
                    "tool_name": "execute_command",
                    "args": {"command": "python -m pytest -q"},
                    "status": "failure",
                    "stdout": "1 failed",
                    "result": {"exit_code": 1},
                },
                {
                    "tool_call_id": "call-test-green",
                    "tool_name": "execute_command",
                    "args": {"command": "python -m pytest -q"},
                    "status": "success",
                    "stdout": "3 passed",
                    "result": {"exit_code": 0},
                },
            ],
        }
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results == (
        {
            "requested": "tests_run",
            "status": "passed",
            "executor_provenance": {
                "source": "qwen_tool_call",
                "tool_call_id": "call-test-green",
                "tool_name": "execute_command",
                "command": "python -m pytest -q",
                "stdout_digest": evidence.verification_results[0]["executor_provenance"][
                    "stdout_digest"
                ],
                "result_digest": evidence.verification_results[0]["executor_provenance"][
                    "result_digest"
                ],
                "exit_code": 0,
            },
            "failure_reason": "",
        },
    )
    assert _evidence_admitted(_motor_program_dict(), evidence)


def test_supplied_qwen_verification_results_cannot_spoof_execution():
    episode = _qwen_episode(
        {
            "status": "success",
            "files_changed": ["src/model.py"],
            "tests_run": ["pytest -q"],
            "evidence": {
                "verification_results": [
                    {
                        "status": "passed",
                        "requested": "tests_run",
                        "executor_provenance": {"tool_call_id": "invented"},
                    }
                ]
            },
        }
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results == ()
    assert not _evidence_admitted(_motor_program_dict(), evidence)


def test_raw_qwen_file_test_and_artifact_claims_cannot_verify_code():
    episode = _qwen_episode(
        {
            "status": "success",
            "summary": "implemented and verified",
            "files_changed": ["src/invented.py"],
            "commands_run": ["pytest -q"],
            "tests_run": ["pytest -q"],
            "artifacts": ["invented artifact"],
            "evidence": {"tests_passed": True},
        }
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.files_changed == ()
    assert evidence.commands_run == ()
    assert evidence.tests_run == ()
    assert evidence.artifacts == ()
    assert evidence.verification_results == ()
    assert not _evidence_admitted(_motor_program_dict(), evidence)


def _web_program_dict() -> dict:
    program = _motor_program_dict()
    program["execution_plan"] = {
        "capability_class": "web_research",
        "required_capabilities": ["source_retrieval"],
        "operations": ["fetch structured primary-source content"],
        "permitted_local_discretion": ["choose query terms"],
        "prohibited_deviations": ["invent source content"],
        "return_to_mind_conditions": ["source unavailable"],
    }
    program["evidence_contract"] = {
        "required_artifacts": ["results"],
        "required_source_proof": ["https://example.test/source"],
        "required_checks": ["fetch_verified"],
        "completion_signals": ["research complete"],
        "disqualifying_failures": ["missing source"],
    }
    return program


def _direct_web_episode(tool_result: dict):
    return SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": _web_program_dict()},
        auto={
            "execution_pack": {
                "action": "EXECUTE_MOTOR_PROGRAM",
                "tool": "web_fetch",
                "execution_inputs": {"motor_transport": "USE_TOOL"},
            },
            "status": "success",
            "outputs": {
                "tool_status": "success",
                "tool_args": {"query": "grounded query"},
                "tool_result": tool_result,
            },
        },
    )


def test_real_successful_web_evidence_verifies_from_structured_result():
    episode = _direct_web_episode(
        {
            "results": [
                {
                    "url": "https://example.test/source",
                    "title": "Primary source",
                    "snippet": "grounded content",
                }
            ]
        }
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results[0]["status"] == "passed"
    assert evidence.verification_results[0]["executor_provenance"]["source"] == "direct_tool"
    assert _evidence_admitted(_web_program_dict(), evidence)


def test_web_missing_source_and_check_content_stays_unverified():
    episode = _direct_web_episode({"results": []})

    evidence = execution_evidence_from_fold(episode)

    assert evidence.verification_results[0]["status"] == "failed"
    assert not _evidence_admitted(_web_program_dict(), evidence)


def test_qwen_result_discards_bridge_goal_setting_keys():
    authority_keys = (
        "program_id",
        "parent_program_id",
        "outcome_id",
        "episode_id",
        "attempt_number",
        "created_at",
        "motor_program",
        "intent",
        "pressure",
        "outcome",
        "objective",
        "acceptance_criteria",
        "constraints",
        "priorities",
        "outcome_contract",
        "pathway",
        "pathway_id",
        "revision",
        "name",
        "method",
        "operational_rationale",
        "failed_predecessor_id",
        "change_evidence",
        "execution_plan",
        "capability_class",
        "operations",
        "permitted_local_discretion",
        "prohibited_deviations",
        "return_to_mind_conditions",
        "evidence_contract",
        "required_artifacts",
        "required_source_proof",
        "required_checks",
        "completion_signals",
        "disqualifying_failures",
        "executor_identity",
        "planning_authority",
        "replacement_authority",
        "replacement_objective",
        "replacement_motor_program",
    )
    raw = {
        "ok": True,
        "summary": "done",
        "tool_calls": [{"tool_name": "run_command", "args": {"command": "pytest -q"}}],
        **{key: "bridge-owned" for key in authority_keys},
    }
    result = normalize_qwen_result(raw, dispatch=_dispatch_with_motor_program()).to_dict()
    for authority_key in authority_keys:
        assert authority_key not in result["raw_bridge_result"]
    assert result["episode_id"] == "ep1"


def test_execution_evidence_uses_real_direct_tool_name_args_and_source():
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": _motor_program_dict()},
        auto={
            "execution_pack": {
                "action": "EXECUTE_MOTOR_PROGRAM",
                "tool": "web_fetch",
                "execution_inputs": {"motor_transport": "USE_TOOL"},
            },
            "status": "success",
            "outputs": {
                "tool_status": "success",
                "tool_args": {"query": "real query"},
                "tool_result": {"url": "https://example.test/source", "title": "Source"},
            },
        },
    )
    evidence = execution_evidence_from_fold(episode)
    assert evidence.tool_calls[0]["tool_name"] == "web_fetch"
    assert evidence.tool_calls[0]["args"] == {"query": "real query"}
    assert evidence.sources_consulted == ("https://example.test/source",)


def test_failed_direct_tool_attempt_retains_factual_call_evidence():
    pack = _execution_pack(action="EXECUTE_MOTOR_PROGRAM", motor_transport="USE_TOOL")
    prompt = build_prompt_pack(pack, _empty_memory())
    result = execute(
        prompt,
        None,
        pack,
        _empty_memory(),
        "ep1",
        runtime_client=object(),
        tool_invoker=lambda tool, args: {"ok": False, "error": "network down"},
    )
    assert result["outputs"]["tool_status"] == "failed"
    assert result["outputs"]["tool_args"] == {
        "query": "run the focused pytest suite",
        "max_results": 3,
    }
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": _motor_program_dict()},
        auto={
            "execution_pack": pack,
            "status": "failed",
            "failure_kind": result["failure_kind"],
            "failure_reason": result["failure_reason"],
            "outputs": result["outputs"],
        },
    )
    evidence = execution_evidence_from_fold(episode)
    assert evidence.status == "failure"
    assert evidence.tool_calls == (
        {
            "tool_name": "web_fetch",
            "args": {"query": "run the focused pytest suite", "max_results": 3},
            "status": "failed",
        },
    )


def test_direct_executor_rate_limit_normalizes_to_transient_failure_kind():
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={"motor_program": _web_program_dict()},
        auto={
            "execution_pack": {
                "action": "EXECUTE_MOTOR_PROGRAM",
                "tool": "web_fetch",
                "execution_inputs": {"motor_transport": "USE_TOOL"},
            },
            "status": "failure",
            "failure_kind": "tool_failed",
            "failure_reason": "HTTP 429: provider throttled request",
            "outputs": {
                "tool_status": "failed",
                "tool_args": {"query": "facts"},
                "tool_error": "rate_limit",
            },
        },
    )

    evidence = execution_evidence_from_fold(episode)

    assert evidence.failure_kind == "rate_limited"


def test_exceptional_direct_tool_attempt_retains_normalized_args_and_status():
    pack = _execution_pack(action="EXECUTE_MOTOR_PROGRAM", motor_transport="USE_TOOL")
    prompt = build_prompt_pack(pack, _empty_memory())

    def raise_after_invocation(tool, args):
        raise RuntimeError("socket closed")

    result = execute(
        prompt,
        None,
        pack,
        _empty_memory(),
        "ep1",
        runtime_client=object(),
        tool_invoker=raise_after_invocation,
    )
    assert result["outputs"]["tool_status"] == "failed"
    assert result["outputs"]["tool_args"] == {
        "query": "run the focused pytest suite",
        "max_results": 3,
    }
