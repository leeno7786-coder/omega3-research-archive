import pytest

from meta_loop.action.run import run
from meta_loop._meta_a_state import MetaAState
from meta_loop.intake.run import MetaAInput
from meta_loop.memory.run import run as run_memory


@pytest.mark.parametrize(
    ("intent", "lane"),
    [("BROWSE", "browser"), ("RESPOND", "agent")],
)
def test_global_a_specialist_route_becomes_a_chat_lane_handoff(intent, lane):
    workflow = (
        {"workflow_id": "workflow-1", "objective": "Find current pricing", "status": "active"}
        if lane == "browser"
        else None
    )
    meta_input = MetaAInput(
        episode_id="chat-episode",
        pressure={
            "reason": "operator_command",
            "contributors": [{
                "id": "operator_command",
                "metadata": {
                    "message": "Find current pricing",
                    "cycle_id": "operator-cycle",
                    "arrival_seq": 11,
                },
            }],
        },
        trajectory_snapshot={},
        action_goal={"episode_id": "chat-episode", "intent": intent},
        browser_workflow=workflow,
        operator_route=lane,
        operator_route_context="the current request",
        thread_id="host-assigned-thread",
    )

    pack = run(meta_input)

    assert pack.action == "RESPONSE"
    handoff = pack.execution_inputs["specialist_handoff"]
    assert handoff["lane"] == lane
    assert handoff["original_cycle_id"] == "operator-cycle"
    assert handoff["thread_id"] == "host-assigned-thread"
    assert handoff["request"] == "Find current pricing"
    if lane == "browser":
        assert handoff["browser_workflow"] == workflow


def test_slash_selected_skill_survives_chat_handoff_for_agent_meta_a():
    meta_input = MetaAInput(
        episode_id="chat-episode",
        pressure={
            "reason": "operator_command",
            "contributors": [{
                "id": "operator_command",
                "metadata": {
                    "message": "Add a health check endpoint",
                    "cycle_id": "operator-cycle",
                    "arrival_seq": 11,
                    "directive_intent": "USE_SKILL",
                    "selected_skill": "repo-helper",
                },
            }],
        },
        trajectory_snapshot={},
        action_goal={"episode_id": "chat-episode", "intent": "RESPOND"},
        operator_route="agent",
        thread_id="host-assigned-thread",
    )

    pack = run(meta_input)

    assert pack.action == "RESPONSE"
    assert pack.execution_inputs["specialist_handoff"]["selected_skill"] == "repo-helper"


@pytest.mark.parametrize(
    ("lane", "intent", "expected_action"),
    [("browser", "BROWSE", "BROWSER_ACTION"), ("agent", "RESPOND", "RESPONSE")],
)
def test_a_specialist_work_item_executes_on_its_lane_instead_of_handing_off_again(
    lane, intent, expected_action
):
    workflow = (
        {"workflow_id": "workflow-1", "objective": "Find current pricing", "status": "active"}
        if lane == "browser"
        else None
    )
    meta_input = MetaAInput(
        episode_id=f"{lane}-episode",
        pressure={
            "reason": "lane_work_dispatch",
            "contributors": [{
                "id": "lane_work_dispatch",
                "metadata": {
                    "lane_work_handoff": True,
                    "lane": lane,
                    "message": "Find current pricing",
                    "original_cycle_id": "operator-cycle",
                },
            }],
        },
        trajectory_snapshot={},
        action_goal={"episode_id": f"{lane}-episode", "intent": intent},
        browser_workflow=workflow,
        operator_route=lane,
    )

    pack = run(meta_input)

    assert pack.action == expected_action
    assert pack.execution_inputs["operator_route"] == lane
    assert "specialist_handoff" not in pack.execution_inputs


def test_selected_skill_handoff_runs_that_skill_inside_the_agent_lane():
    meta_input = MetaAInput(
        episode_id="agent-episode",
        pressure={
            "reason": "lane_work_dispatch",
            "contributors": [{
                "id": "lane_work_dispatch",
                "metadata": {
                    "lane_work_handoff": True,
                    "lane": "agent",
                    "message": "Add a health check endpoint",
                    "original_cycle_id": "operator-cycle",
                    "selected_skill": "repo-helper",
                },
            }],
        },
        trajectory_snapshot={},
        action_goal={"episode_id": "agent-episode", "intent": "USE_SKILL"},
        operator_route="agent",
    )

    pack = run(meta_input)

    assert pack.action == "USE_SKILL"
    assert pack.execution_inputs["selected_skill"] == "repo-helper"


def test_chat_handoff_does_not_recall_coding_memory_before_a_run_anchor_exists():
    state = MetaAState(
        mi=None,
        episode_id="chat-episode",
        intent="RESPOND",
        action="RESPONSE",
        source={"episode_id": "chat-episode", "intent": "RESPOND"},
        text="Change the active agent session to Rust",
        target_str="Change the active agent session to Rust",
        constraints={},
        meta={},
        execution_inputs={
            "operator_route": "agent",
            "specialist_handoff": {"lane": "agent", "original_cycle_id": "operator-cycle"},
        },
    )

    result = run_memory(state)

    assert result.memory_required is False
    assert result.memory_tools == ()
