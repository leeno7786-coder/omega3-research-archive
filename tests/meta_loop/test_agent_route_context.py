"""The intent router's follow-up binding survives into the agent execution pack."""

from types import SimpleNamespace

from meta_loop.action.run import run as action_run
from meta_loop.intake.run import meta_a_input_from_episode
from meta_loop.tool.run import run as tool_run


def test_agent_followup_context_reaches_auto_execution_inputs():
    context = 'CODING_AGENT: worked in the operator\'s project on "create a basic todo app"'
    episode = SimpleNamespace(
        episode_id="ep-followup",
        global_={
            "intent_decision": {
                "episode_id": "ep-followup",
                "intent": "RESPOND",
                "confidence": 1.0,
                "reason": "routed",
                "omega_lane": "agent",
            },
            "operator_route": {"lane": "agent", "context": context},
        },
        pressure={
            "opened_by": "operator_query",
            "contributors": [
                {"id": "operator_query", "metadata": {
                    "message": "yes, create those files", "cycle_id": "agent-bubble",
                    "thread_id": "agent-thread", "turn_id": "agent-turn",
                }}
            ],
        },
    )
    meta_input = meta_a_input_from_episode(episode)

    assert meta_input is not None
    assert meta_input.operator_route == "agent"
    assert meta_input.operator_route_context == context

    state = tool_run(action_run(meta_input))

    assert state.execution_inputs["operator_route"] == "agent"
    assert state.execution_inputs["operator_route_context"] == context


def test_an_agent_task_always_recalls_the_dev_lobe():
    """Operator 2026-09-15: the trusted workspaces live in dev memory; every agent task recalls them."""
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.intake.run import MetaAInput
    from meta_loop.memory.run import run as memory_run

    def state_for(text):
        return MetaAState(
            mi=MetaAInput(episode_id="ep-agent-recall", pressure={"contributors": [], "aggregate_pressure": 0.0},
                          trajectory_snapshot={}, action_goal={"episode_id": "ep-agent-recall", "intent": "RESPOND"}),
            episode_id="ep-agent-recall", intent="RESPOND", action="RESPONSE",
            source={"episode_id": "ep-agent-recall", "intent": "RESPOND"}, text=text, target_str=text,
            constraints={"planning_only": True, "no_inference_in_meta": True, "no_execution_in_meta": True,
                         "no_model_fields": True},
            meta={"chat_intent": "freeform"}, execution_intent="plan for global intent RESPOND",
            execution_inputs={"operator_route": "agent"},
        )

    task = memory_run(state_for("Build a basic todo app with React"))
    assert task.memory_required is True
    dev_tools = [tool for tool in task.memory_tools if tool.get("tool") == "remember_dev"]
    assert len(dev_tools) == 1 and dev_tools[0]["query"]
    assert dev_tools[0]["lane"] in task.memory_scope

    inspection = memory_run(state_for("Inspect the current directory"))
    assert inspection.memory_tools == () and inspection.memory_required is False
