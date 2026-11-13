"""Slice 2 B3 — MetaAInput assembly from episode."""

from __future__ import annotations

from pressure_loop.contributors import make_contributor
from pressure_loop.episode import EpisodeStore
from meta_loop.intake.run import meta_a_input_from_episode
from meta_loop.action.run import run as run_meta_action
from meta_loop.tool.run import run as run_meta_tool
from meta_loop.memory.run import run as run_meta_memory
from meta_loop.pack.run import execution_action_pack_to_dict, run as run_meta_pack


def test_meta_a_input_from_slice1_episode():
    store = EpisodeStore()
    c = make_contributor("operator", "operator_command", 1.0, {"message": "Learn Python"})
    pressure = {
        "aggregate": 1.0,
        "opened_by": "operator_command",
        "neuromod_state": {
            "dopamine": -0.2,
            "serotonin": 0.15,
            "stress": 0.4,
            "spike": 0.0,
        },
        "contributors": [
            {
                "source": c.source,
                "type": c.type,
                "weight": c.weight,
                "metadata": dict(c.metadata),
            }
        ],
    }
    ep = store.open(
        pressure=pressure,
        opened_by="operator_command",
        pipeline_id=0,
        work_kind="operator_turn",
    )
    ep.global_["action_goal"] = {
        "episode_id": ep.episode_id,
        "intent": "LEARN",
        "target": "Python",
        "confidence": 0.9,
        "reason": "opened_by=operator_command",
    }

    mi = meta_a_input_from_episode(ep)
    assert mi is not None
    assert mi.episode_id == ep.episode_id
    assert mi.action_goal["intent"] == "LEARN"
    assert mi.pressure["reason"] == "operator_command"
    assert len(mi.pressure["contributors"]) == 1
    assert mi.pressure["neuromod_state"]["serotonin"] == 0.15

    action = run_meta_action(mi)
    assert action.execution_inputs["neuromod_state"] == pressure["neuromod_state"]


def test_meta_a_input_missing_action_goal_returns_none():
    store = EpisodeStore()
    ep = store.open(
        pressure={"aggregate": 0.1, "opened_by": "idle_probe_aggregate"},
        opened_by="idle_probe_aggregate",
        pipeline_id=0,
        work_kind="autonomous",
    )
    assert meta_a_input_from_episode(ep) is None


def test_browser_route_survives_the_live_meta_action_tool_pack():
    """Global A's routed lane must reach AUTO after Meta tool selection."""
    store = EpisodeStore()
    c = make_contributor(
        "operator",
        "operator_command",
        1.0,
        {"message": "open a browser tab and navigate to huggingface",
         "cycle_id": "browser-bubble", "thread_id": "browser-thread"},
    )
    ep = store.open(
        pressure={
            "aggregate": 1.0,
            "opened_by": "operator_command",
            "contributors": [
                {
                    "source": c.source,
                    "type": c.type,
                    "weight": c.weight,
                    "metadata": dict(c.metadata),
                }
            ],
        },
        opened_by="operator_command",
        pipeline_id=0,
        work_kind="operator_turn",
    )
    ep.global_["action_goal"] = {
        "episode_id": ep.episode_id,
        "intent": "RESPOND",
        "confidence": 1.0,
        "reason": "browser capability route",
    }
    ep.global_["operator_route"] = {"route": "BROWSER_AGENT", "source": "router", "lane": "browser"}
    ep.global_["browser_workflow"] = {
        "workflow_id": "browser-workflow-1",
        "objective": "Navigate to Hugging Face",
        "status": "active",
    }

    mi = meta_a_input_from_episode(ep)
    assert mi is not None
    state = run_meta_tool(run_meta_action(mi))
    state.meta["chat_intent"] = "question"
    state.memory_required = True
    state.memory_answer_required = True
    state.memory_reason = "stale classifier memory contract"
    state.memory_scope = ("knowledge",)
    state.memory_query = "huggingface"
    state.memory_delivery_mode = "inference_tool"
    state.memory_tools = ({"tool": "remember_knowledge"},)
    state = run_meta_memory(state)
    pack = execution_action_pack_to_dict(run_meta_pack(state))

    assert pack["execution_inputs"]["operator_route"] == "browser"
    assert pack["execution_inputs"]["specialist_handoff"]["browser_workflow"] == ep.global_["browser_workflow"]
    assert state.memory_required is False
    assert state.memory_answer_required is False
    assert state.memory_reason is None
    assert state.memory_scope == ()
    assert state.memory_query is None
    assert state.memory_delivery_mode is None
    assert state.memory_tools == ()


def test_workspace_inspection_route_drops_stale_recall_before_listing():
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.intake.run import MetaAInput

    state = MetaAState(
        mi=MetaAInput(
            episode_id="ep-workspace-route",
            pressure={"contributors": [], "aggregate_pressure": 0.0},
            trajectory_snapshot={},
            action_goal={"episode_id": "ep-workspace-route", "intent": "RESPOND"},
        ),
        episode_id="ep-workspace-route",
        intent="RESPOND",
        action="RESPONSE",
        source={},
        text="Inspect the current directory",
        target_str="Inspect the current directory",
        constraints={},
        meta={},
        execution_inputs={"operator_route": "agent"},
        memory_required=True,
        memory_answer_required=True,
        memory_reason="stale classifier memory contract",
        memory_scope=("knowledge",),
        memory_query="old conversation",
        memory_delivery_mode="inference_tool",
        memory_tools=({"tool": "remember_knowledge"},),
    )

    out = run_meta_memory(state)

    assert out.memory_required is False
    assert out.memory_answer_required is False
    assert out.memory_reason is None
    assert out.memory_scope == ()
    assert out.memory_query is None
    assert out.memory_delivery_mode is None
    assert out.memory_tools == ()
