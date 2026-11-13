"""Meta A continues an operator's terminal-agent task on the agent RESPONSE path."""

from meta_loop.action.run import run as action_run
from meta_loop.intake.run import MetaAInput


def _input(meta, intent="USE_TOOL", lane="agent"):
    return MetaAInput(
        episode_id="ep-2",
        pressure={"contributors": [{"id": "omega_successor", "detail": meta}]},
        trajectory_snapshot={},
        action_goal={"episode_id": "ep-2", "intent": intent, "omega_lane": lane},
    )


def test_an_agent_successor_runs_its_next_batch_on_the_agent_response_path():
    observations = [{"tool": "list_directory", "args": "{}", "status": "success", "output": "FILE\ta.py", "error": ""}]
    state = action_run(_input({
        "original_cycle_id": "cyc-1", "original_objective": "tidy the repo", "message": "tidy the repo",
        "reason": "delete the empty folder", "agent_observations": observations,
    }))

    assert state.action == "RESPONSE"
    inputs = state.execution_inputs
    assert inputs["agent_continuation"] is True and inputs["operator_route"] == "agent"
    assert inputs["query"] == "tidy the repo"
    assert inputs["agent_next_step"] == "delete the empty folder"
    assert inputs["agent_observations"] == observations


def test_use_tool_without_operator_lineage_keeps_its_own_action():
    state = action_run(_input({"original_cycle_id": "", "message": "study caching"}))

    assert state.action == "USE_TOOL"
    assert "agent_continuation" not in state.execution_inputs
