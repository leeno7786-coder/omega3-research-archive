"""The attempt's execution contract is projected from Meta A's validated pack, never authored by a model."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from schema.skill_execution_pack import build_skill_execution_pack

_OPENED_AT = "2026-09-14T12:00:00+00:00"


def _episode(action: str, inputs: dict, *, tool: str | None = None, episode_id: str = "ep-contract"):
    return SimpleNamespace(
        episode_id=episode_id,
        opened_at=_OPENED_AT,
        meta={"execution_action_pack": {
            "episode_id": episode_id, "action": action, "tool": tool,
            "tool_reason": "Meta A selected this capability", "execution_inputs": inputs,
            "source_action_goal": {"episode_id": episode_id},
        }},
    )


def _skill_episode(*, objective="refactor the importer module", evidence=("diff_summary", "tests_run"),
                   criteria=("refactor_applied", "tests_pass"), skill_class="refactor_skill"):
    decision = SimpleNamespace(
        selected=True, skill_id="omega-refactor-surgeon", skill_name="omega-refactor-surgeon",
        skill_class=skill_class, execution_kind="REFACTOR", source_path="skills/refactor/SKILL.md",
        allowed_tools=("write_file", "run_tests"), forbidden_tools=(), safety_class="write_safe",
        requires_confirmation_for=(), limits={"max_steps": 40}, evidence_required=evidence,
        completion_criteria=criteria, output_schema={"type": "object"},
    )
    pack = build_skill_execution_pack(episode_id="ep-contract", objective=objective, decision=decision,
                                      source_action_goal={"episode_id": "ep-contract"}).to_dict()
    return _episode("USE_SKILL", {"objective": objective, "skill_execution_pack": pack})


@pytest.fixture(autouse=True)
def _live_contract_attachment(monkeypatch):
    monkeypatch.setattr("meta_loop.execution_contract.motor_programs_enabled", lambda *_a: True)
    monkeypatch.setattr("omega_loop.motor.run.motor_programs_shadow_mode", lambda: False)


def test_skill_pack_projects_a_deterministic_contract():
    from meta_loop.execution_contract import execution_contract_for

    program = execution_contract_for(_skill_episode())

    assert (program.program_id, program.episode_id, program.attempt_number, program.created_at) == (
        "mp_ep-contract_01", "ep-contract", 1, _OPENED_AT)
    assert program.outcome_contract.objective == "refactor the importer module"
    assert program.outcome_contract.acceptance_criteria == ("refactor_applied", "tests_pass")
    assert program.evidence_contract.required_checks == ("diff_summary", "tests_run")
    assert program.evidence_contract.completion_signals == ("refactor_applied", "tests_pass")
    assert program.execution_plan.capability_class == "refactor_skill"
    assert program.to_dict() == execution_contract_for(_skill_episode()).to_dict()


def test_bound_contract_keeps_the_selected_skill_terms_authoritative():
    from meta_loop.execution_contract import bind_execution_contract

    episode = _skill_episode()
    attached = bind_execution_contract(episode)

    skill_pack = episode.meta["execution_action_pack"]["execution_inputs"]["skill_execution_pack"]
    assert episode.meta["motor_program"] == attached
    assert skill_pack["objective"] == "refactor the importer module"
    assert skill_pack["evidence_required"] == ["diff_summary", "tests_run"]
    assert skill_pack["completion_criteria"] == ["refactor_applied", "tests_pass"]


def test_web_fetch_contract_uses_the_fetch_check_and_the_query_as_its_operation():
    from meta_loop.execution_contract import execution_contract_for

    program = execution_contract_for(
        _episode("USE_TOOL", {"query": "Dijkstra shortest path", "domain": "algorithms"}, tool="web_fetch"))

    assert program.execution_plan.capability_class == "web_research"
    assert program.execution_plan.operations == ("Dijkstra shortest path",)
    assert program.evidence_contract.required_checks == ("fetch",)


def test_successor_pressure_contract_inherits_outcome_parent_and_attempt():
    from meta_loop.execution_contract import execution_contract_for

    episode = _episode("EXECUTE_MOTOR_PROGRAM", {
        "objective": {"tests": "passing"},
        "motor_transport": "USE_TOOL",
        "emergent_pressure": {
            "outcome_id": "out-continuous",
            "failed_program_ids": ["mp-first", "mp-second"],
            "resolution_conditions": ["required tests pass"],
            "proposed_action": {"intended_transformation": "find a verified derivation",
                                "required_capabilities": ["web_research"]},
        },
    }, tool="web_fetch")

    program = execution_contract_for(episode)

    assert (program.outcome_id, program.parent_program_id, program.attempt_number, program.program_id) == (
        "out-continuous", "mp-second", 3, "mp_ep-contract_03")
    assert program.execution_plan.operations == ("find a verified derivation",)
    assert program.outcome_contract.acceptance_criteria == ("required tests pass",)


def test_unprovable_code_contract_runs_uncontracted_and_says_so(monkeypatch):
    from meta_loop.execution_contract import bind_execution_contract

    events = []
    monkeypatch.setattr("guardian.terminal_feedback.emit_data",
                        lambda system, event, **fields: events.append((system, event, fields)))
    episode = _skill_episode(objective="build a parser and test it", evidence=("api_patterns_matched",))

    assert bind_execution_contract(episode) is None
    assert "motor_program" not in episode.meta
    assert [event for _system, event, _fields in events] == ["execution_contract_unavailable"]
    assert events[0][2]["episode_id"] == "ep-contract"


def test_ineligible_or_disabled_episodes_get_no_contract(monkeypatch):
    from meta_loop.execution_contract import bind_execution_contract, execution_contract_for

    assert execution_contract_for(_episode("RESPONSE", {"message": "hello"})) is None
    monkeypatch.setattr("meta_loop.execution_contract.motor_programs_enabled", lambda *_a: False)
    episode = _skill_episode()
    assert bind_execution_contract(episode) is None
    assert "motor_program" not in episode.meta


def test_pack_from_another_episode_fails_hard():
    from meta_loop.execution_contract import bind_execution_contract

    episode = _skill_episode()
    episode.meta["execution_action_pack"]["episode_id"] = "stale-episode"
    with pytest.raises(ValueError, match="episode_id"):
        bind_execution_contract(episode)
