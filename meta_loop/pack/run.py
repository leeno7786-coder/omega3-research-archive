"""Meta A pack phase — assemble ExecutionActionPack + validate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

from meta_loop._meta_a_state import MetaAState


def _banned_keys() -> frozenset[str]:
    from pressure_loop.policy import meta_pack_banned_keys

    return meta_pack_banned_keys()


_REQUIRED_CONSTRAINT_FLAGS: Dict[str, bool] = {
    "planning_only": True,
    "no_inference_in_meta": True,
    "no_execution_in_meta": True,
    "no_model_fields": True,
}


@dataclass(frozen=True)
class ExecutionActionPack:
    episode_id: str
    action: str
    tool: Optional[str]
    tool_reason: Optional[str]
    memory_required: bool
    memory_answer_required: bool
    memory_reason: Optional[str]
    memory_scope: tuple
    memory_query: Optional[str]
    memory_delivery_mode: Optional[str]
    memory_tools: tuple
    execution_intent: str
    execution_inputs: dict
    execution_constraints: dict
    source_action_goal: dict


def _iter_key_paths(obj: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            yield key.split(".")[-1]
            yield from _iter_key_paths(v, key)
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            yield from _iter_key_paths(item, f"{prefix}[{i}]")


def validate_execution_action_pack(pack: Any) -> None:
    """Reject packs containing banned model/inference keys (recursive)."""
    if isinstance(pack, ExecutionActionPack):
        data = execution_action_pack_to_dict(pack)
    elif isinstance(pack, Mapping):
        data = dict(pack)
    else:
        raise TypeError(f"expected Mapping or ExecutionActionPack, got {type(pack)}")

    episode_id = str(data.get("episode_id") or "").strip()
    if not episode_id:
        raise ValueError("execution action pack episode_id is required")
    source_action_goal = data.get("source_action_goal")
    if not isinstance(source_action_goal, Mapping):
        raise ValueError("source_action_goal must be a mapping")
    source_episode_id = str(source_action_goal.get("episode_id") or "").strip()
    if not source_episode_id:
        raise ValueError("source_action_goal episode_id is required")
    if source_episode_id != episode_id:
        raise ValueError(
            "source_action_goal episode_id must match execution action pack episode_id"
        )

    for key in _iter_key_paths(data):
        if key in _banned_keys():
            raise ValueError(f"banned key in execution action pack: {key}")

    from meta_loop.action.run import META_ACTIONS

    if data.get("action") not in META_ACTIONS:
        raise ValueError(f"unknown meta action: {data.get('action')!r}")

    constraints = data.get("execution_constraints") or {}
    if not isinstance(constraints, dict):
        raise ValueError("execution_constraints must be a dict")
    for flag, required in _REQUIRED_CONSTRAINT_FLAGS.items():
        if constraints.get(flag) is not required:
            raise ValueError(f"execution_constraints missing or wrong value for {flag!r}")

    if data.get("tool") and not data.get("tool_reason"):
        raise ValueError("tool_reason required when tool is set")
    if data.get("action") == "BROWSER_ACTION":
        from schema.browser_perception import BrowserPerceptionPack

        inputs = data.get("execution_inputs") or {}
        workflow = inputs.get("browser_workflow")
        if not isinstance(workflow, dict) or not workflow.get("workflow_id") or (
            inputs.get("browser_workflow_id") != workflow["workflow_id"]
        ):
            raise ValueError("browser workflow identity mismatch")
        if not str(inputs.get("objective") or "").strip():
            raise ValueError("browser run requires the workflow objective")
        if data.get("tool") != "browser_agent" or any(
            key in inputs for key in ("action", "selector", "tool_calls")
        ):
            raise ValueError("Meta browser pack cannot select a browser action")
        BrowserPerceptionPack.from_dict(inputs.get("browser_perception"))
    if data.get("action") == "WINDOW_REFLECTION" and (data.get("tool") or data.get("memory_required")):
        raise ValueError("window reflection selects no tool and no memory recall")
    if data.get("memory_answer_required") and not data.get("memory_required"):
        raise ValueError("memory_answer_required requires memory_required")
    if data.get("memory_required"):
        if not data.get("memory_reason"):
            raise ValueError("memory_reason required when memory_required")
        if not data.get("memory_query"):
            raise ValueError("memory_query required when memory_required")
        tools = data.get("memory_tools")
        if not tools or not isinstance(tools, (list, tuple)) or len(tools) == 0:
            raise ValueError("memory_tools required when memory_required")
        # One OR MORE Meta-routed remember_* tools (e.g. freeform recalls identity +
        # persona). Each entry must be a remember_* tool — the recall executor runs
        # them all and aggregates the evidence.
        for spec in tools:
            name = spec.get("tool") if isinstance(spec, dict) else None
            if not (isinstance(name, str) and name.startswith("remember_")):
                raise ValueError("each memory_tools entry must be a Meta-routed remember_* tool")


def execution_action_pack_to_dict(pack: ExecutionActionPack) -> Dict[str, Any]:
    d = asdict(pack)
    d["memory_scope"] = list(pack.memory_scope)
    d["memory_tools"] = list(pack.memory_tools)
    return d


def run(state: MetaAState) -> ExecutionActionPack:
    action = state.action
    if state.tool is None and state.execution_intent == "SKIP_NO_TOPIC":
        action = "IDLE"
    pack = ExecutionActionPack(
        episode_id=state.episode_id,
        action=action,
        tool=state.tool,
        tool_reason=state.tool_reason,
        memory_required=state.memory_required,
        memory_answer_required=state.memory_answer_required,
        memory_reason=state.memory_reason,
        memory_scope=state.memory_scope,
        memory_query=state.memory_query,
        memory_delivery_mode=state.memory_delivery_mode,
        memory_tools=state.memory_tools,
        execution_intent=state.execution_intent,
        execution_inputs=state.execution_inputs,
        execution_constraints=state.constraints,
        source_action_goal=state.source,
    )
    validate_execution_action_pack(pack)
    try:
        from guardian.terminal_feedback import emit_data

        operator_route = str(pack.execution_inputs.get("operator_route") or "").strip()
        emit_data(
            "meta",
            "execution_action_pack",
            episode_id=state.episode_id,
            summary=f"action={pack.action}",
            action=pack.action,
            tool=pack.tool,
            inputs=dict(pack.execution_inputs),
            operator_route=operator_route or None,
            memory_required=pack.memory_required,
            memory_answer_required=pack.memory_answer_required,
        )
    except Exception:
        pass
    return pack
