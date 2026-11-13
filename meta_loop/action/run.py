"""Meta A action phase — intent → meta action + base execution fields."""

from __future__ import annotations

from copy import deepcopy
from typing import Dict

from meta_loop._meta_a_state import MetaAState
from meta_loop.intake.run import MetaAInput, _contributors_metadata, _dominant_text

META_ACTIONS = (
    "USE_TOOL",
    "RESPONSE",
    "PROACTIVE_RESPONSE",
    "ASK_OPERATOR",
    "IMAGE_CREATION",
    "DOCUMENT_CREATION",
    "IDLE",
    # USE_SKILL (Phase 6): skill-mediated agentic execution — Meta A selects the ExecutionSkill,
    # AUTO loads its SKILL.md and dispatches qwen inside it. RETIRES the legacy APPLY action.
    "USE_SKILL",
    "EXECUTE_MOTOR_PROGRAM",
    # External app work is one explicit signed MCP plugin operation. AUTO
    # validates the plugin/app/capability tuple; Meta never dispatches a provider.
    "PLUGIN_OPERATION",
    "BROWSER_ACTION",
    # A window episode's structural action: AUTO attests the carried locked window;
    # the reflection itself is Omega's window-lane fold after Global B.
    "WINDOW_REFLECTION",
)

_INTENT_TO_ACTION_DEFAULT: Dict[str, str] = {
    "BROWSE": "BROWSER_ACTION",
    "REFLECT_WINDOW": "WINDOW_REFLECTION",
    "RESPOND": "RESPONSE",
    "RESOLVE_QUERY": "RESPONSE",
    "PROACTIVE_RESPONSE": "PROACTIVE_RESPONSE",
    # The system-generated query keeps its own action end to end — clean
    # route separation (operator directive 2026-06-12).
    "ASK_OPERATOR": "ASK_OPERATOR",
    "LEARN": "USE_TOOL",
    "USE_TOOL": "USE_TOOL",
    "IMAGE_CREATE": "IMAGE_CREATION",
    "DOCUMENT_CREATE": "DOCUMENT_CREATION",
    "IDLE": "IDLE",
    # Phase 6: USE_SKILL is the agentic execution action. Legacy APPLY (Omega store / directive)
    # shims to USE_SKILL — Global A canonicalizes APPLY->USE_SKILL when routing is ON.
    "USE_SKILL": "USE_SKILL",
    "APPLY": "USE_SKILL",
    "PURSUE_OUTCOME": "EXECUTE_MOTOR_PROGRAM",
    "PLUGIN_OPERATION": "PLUGIN_OPERATION",
}

# Back-compat alias for tests/docs that import _INTENT_TO_ACTION
_INTENT_TO_ACTION = _INTENT_TO_ACTION_DEFAULT


def _intent_to_action_map() -> Dict[str, str]:
    from pressure_loop.policy import meta_intent_to_action

    merged = dict(_INTENT_TO_ACTION_DEFAULT)
    merged.update(meta_intent_to_action())
    merged["BROWSE"] = "BROWSER_ACTION"
    merged["REFLECT_WINDOW"] = "WINDOW_REFLECTION"
    return merged


_REQUIRED_CONSTRAINT_FLAGS: Dict[str, bool] = {
    "planning_only": True,
    "no_inference_in_meta": True,
    "no_execution_in_meta": True,
    "no_model_fields": True,
}

_SKILL_MOTOR_CAPABILITIES = frozenset(
    {"code_execution", "building", "testing", "patching", "project_restore"}
)


def _motor_transport(emergent_pressure: dict) -> str:
    action = dict(emergent_pressure.get("proposed_action") or {})
    capabilities = {str(value).strip() for value in action.get("required_capabilities") or ()}
    return "USE_SKILL" if capabilities & _SKILL_MOTOR_CAPABILITIES else "USE_TOOL"


def _echo_action_goal(action_goal: dict) -> dict:
    return {
        "episode_id": str(action_goal.get("episode_id") or ""),
        "intent": str(action_goal.get("intent") or ""),
        "confidence": float(action_goal.get("confidence") or 0.0),
        "reason": str(action_goal.get("reason") or ""),
        "omega_lane": str(action_goal.get("omega_lane") or ""),
    }


def run(mi: MetaAInput) -> MetaAState:
    ag = dict(mi.action_goal or {})
    intent = str(ag.get("intent") or "IDLE")
    action = _intent_to_action_map().get(intent, "IDLE")
    source = _echo_action_goal(ag)
    text = _dominant_text(mi)
    # Global A no longer carries a target — the operator/topic text reaches Meta A
    # via the dominant contributor message (pressure), not a goal target.
    target_str = text
    meta = _contributors_metadata(mi)
    execution_intent = f"plan for global intent {intent}"
    execution_inputs: dict = {}

    # Global A classifies this turn inside Chat. Only after that classification
    # does AUTO admit the specialist work to its dedicated lane; the Chat episode
    # itself must not execute Browser or Coding Agent work inline.
    opened_by = str((mi.pressure or {}).get("reason") or "")
    operator_route_handoff = (
        mi.operator_route in {"browser", "agent"}
        and opened_by in {"operator_query", "operator_command", "operator_response"}
        and not bool(meta.get("lane_work_handoff"))
    )

    if intent == "BROWSE":
        workflow = mi.browser_workflow
        if not isinstance(workflow, dict) or not workflow.get("workflow_id") or not workflow.get("objective"):
            raise ValueError("BROWSE requires Global A's bound browser workflow")
        # The run's objective is the workflow's; the operator's newest message (or Omega's
        # reason on a successor) is the current instruction the run starts from.
        execution_inputs = {"objective": str(workflow["objective"]),
                            "current_instruction": str(text or meta.get("reason") or workflow["objective"])[:4000]}
        execution_intent = "Run the browser agent toward the operator's objective"
    elif intent == "REFLECT_WINDOW":
        # Structural: no tool, no memory recall and no inference. The staged window's
        # identity rides its contributor so AUTO can attest the window admission carried.
        window_id = str(meta.get("window_id") or "").strip()
        if not window_id:
            raise ValueError("REFLECT_WINDOW requires the staged window identity")
        execution_inputs = {"window_id": window_id,
                            "episode_count": int(meta.get("episode_count") or 0)}
        execution_intent = "Attach the locked reflection window for Omega's window fold"
    elif intent == "RESOLVE_QUERY":
        execution_inputs = {"resolution_context": dict(meta)}
        execution_intent = "Plan query resolution from contributor context"
    elif intent == "PROACTIVE_RESPONSE":
        execution_intent = "Plan proactive response (no text generation in Meta A)"
        seed = str(meta.get("message") or meta.get("question") or text or "").strip()
        if seed:
            execution_inputs = {"message": seed, "context": seed}
        if str(meta.get("presentation_kind") or "") == "PRESENT_RESULT":
            execution_inputs.update(
                {
                    "presentation_kind": "PRESENT_RESULT",
                    "proactive_thread_id": str(meta.get("proactive_thread_id") or ""),
                    "proactive_policy_id": str(meta.get("proactive_policy_id") or ""),
                    "proactive_application_id": str(meta.get("proactive_application_id") or ""),
                    "operator_policy": deepcopy(meta.get("operator_policy") or {}),
                }
            )
    elif intent == "ASK_OPERATOR":
        execution_intent = "Plan the system's query to the operator (no text generation in Meta A)"
        seed = str(meta.get("question") or meta.get("message") or text or "").strip()
        if seed:
            execution_inputs = {"question": seed, "message": seed}
    elif intent == "IDLE":
        execution_intent = "No meaningful pressure — idle planning stub"
    elif intent == "USE_SKILL":
        execution_intent = (
            "Plan the skill-mediated dispatch — Meta A selects the ExecutionSkill; AUTO "
            "loads its SKILL.md and dispatches qwen inside it (no execution in Meta A)"
        )
        seed = str(text or meta.get("message") or "").strip()
        execution_inputs = {"objective": seed, "message": seed}
        if str(meta.get("selected_skill") or "").strip():
            # The operator chose the skill by slash command; skill selection binds it.
            execution_inputs["selected_skill"] = str(meta["selected_skill"]).strip()
    elif intent == "USE_TOOL" and source["omega_lane"] == "agent" and str(meta.get("original_cycle_id") or ""):
        # The operator's terminal-agent task continues (operator 2026-09-14): its next tool
        # batch runs on the same agent RESPONSE path its first step used, carrying what the
        # earlier batches found.
        action = "RESPONSE"
        execution_inputs = {
            "query": str(meta.get("original_objective") or text or "").strip(),
            "agent_continuation": True,
            "job_cycle_id": str(meta.get("job_cycle_id") or meta["original_cycle_id"]),
            "agent_next_step": str(meta.get("reason") or meta.get("desired_state") or "").strip(),
            "agent_observations": deepcopy(list(meta.get("agent_observations") or ())),
        }
        if str(meta.get("agent_job_id") or "").strip():
            # The job the earlier batch claimed; AUTO re-owns it instead of starting a second.
            execution_inputs["job_id"] = str(meta["agent_job_id"]).strip()
        execution_intent = "Plan the next terminal-agent tool batch (no execution in Meta A)"
    elif intent == "PLUGIN_OPERATION":
        seed = str(text or meta.get("message") or "").strip()
        operation = meta.get("plugin_operation")
        execution_inputs = {"message": seed, "plugin_objective": seed}
        hint = meta.get("plugin_hint")
        if isinstance(hint, str) and hint.strip():
            execution_inputs["plugin_hint"] = hint.strip()
        if isinstance(operation, dict):
            # The operation is an explicit UI/user payload.  AUTO validates
            # the signed tuple again; Meta only carries it without inferring a
            # provider or capability from prose.
            execution_inputs["plugin_operation"] = deepcopy(operation)
        if (
            meta.get("ingress_class") == "authority_request"
            and meta.get("decision") in {"allow", "cancel"}
        ):
            execution_inputs["plugin_permission_contributor"] = deepcopy(meta)
        execution_intent = "Plan generic plugin operation in AUTO"
    elif intent == "PURSUE_OUTCOME":
        from schema.emergent_pressure import EmergentPressure

        payload = meta.get("emergent_pressure")
        if not isinstance(payload, dict):
            raise ValueError("PURSUE_OUTCOME requires emergent_pressure")
        EmergentPressure.from_dict(payload)
        execution_intent = "Plan deterministic execution of Omega's emergent pressure"
        execution_inputs = {
            "objective": deepcopy(payload.get("desired_state")),
            "emergent_pressure": deepcopy(payload),
            "motor_transport": _motor_transport(payload),
        }

    if operator_route_handoff:
        cycle_id = str(meta.get("cycle_id") or "").strip()
        thread_id = str(meta.get("thread_id") or mi.thread_id or "").strip()
        request = str(meta.get("message") or meta.get("text") or text or "").strip()
        if not cycle_id or not thread_id or not request:
            raise ValueError("specialist lane handoff requires the originating chat cycle, thread, and request")
        handoff = {
            "lane": mi.operator_route,
            "original_cycle_id": cycle_id,
            "thread_id": thread_id,
            "request": request,
            "route_context": str(mi.operator_route_context or ""),
            "arrival_seq": int(meta.get("arrival_seq") or 0),
        }
        selected_skill = str(meta.get("selected_skill") or "").strip()
        if selected_skill:
            handoff["selected_skill"] = selected_skill
        if mi.operator_route == "browser":
            if not isinstance(mi.browser_workflow, dict):
                raise ValueError("Browser lane handoff requires Global A's bound workflow")
            handoff["browser_workflow"] = deepcopy(mi.browser_workflow)
        execution_inputs["specialist_handoff"] = handoff
        action = "RESPONSE"
        execution_intent = f"Persist the Global A {mi.operator_route} handoff before acknowledging in Chat"

    # Propagate the chosen intent and the overall pressure level down into
    # execution_inputs so that AUTO execution and downstream skills have them.
    agg_pressure = float((mi.pressure or {}).get("aggregate_pressure", 0.0))
    execution_inputs["intent"] = intent
    execution_inputs["aggregate_pressure"] = agg_pressure
    execution_inputs["neuromod_state"] = deepcopy((mi.pressure or {}).get("neuromod_state") or {})
    if meta.get("chat_permission_mode"):
        execution_inputs["chat_permission_mode"] = str(meta["chat_permission_mode"])
    if str(meta.get("job_id") or "").strip() and not execution_inputs.get("job_id"):
        # An answer that resumes a paused agent task runs inside the job it already owns.
        execution_inputs["job_id"] = str(meta["job_id"]).strip()
    if mi.operator_route:
        execution_inputs["operator_route"] = mi.operator_route
    if meta.get("job_cycle_id"):
        execution_inputs["job_cycle_id"] = str(meta["job_cycle_id"])
    if mi.operator_route_context:
        execution_inputs["operator_route_context"] = mi.operator_route_context
    if execution_inputs.get("agent_continuation"):
        execution_inputs["operator_route"] = "agent"
    # Browser workflow identity/receipt is host coordination state.  Carry it
    # through Meta unchanged so AUTO can reuse the same tab across turns;
    # episodic memory deliberately does not receive these fields.
    if meta.get("browser_workflow_id"):
        execution_inputs["browser_workflow_id"] = str(meta["browser_workflow_id"])
    if isinstance(meta.get("browser_workflow"), dict):
        execution_inputs["browser_workflow"] = deepcopy(meta["browser_workflow"])
    if intent == "BROWSE":
        execution_inputs["browser_workflow"] = deepcopy(mi.browser_workflow)
        execution_inputs["browser_workflow_id"] = mi.browser_workflow["workflow_id"]

    return MetaAState(
        mi=mi,
        episode_id=str(mi.episode_id or ag.get("episode_id") or ""),
        intent=intent,
        action=action,
        source=source,
        text=text,
        target_str=target_str,
        constraints=dict(_REQUIRED_CONSTRAINT_FLAGS),
        meta=meta,
        execution_intent=execution_intent,
        execution_inputs=execution_inputs,
    )
