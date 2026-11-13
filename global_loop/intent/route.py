"""Global A's operator route: the intent router decides, or Global A reasons over its abstention.

Every operator chat turn that carries no explicit selection is routed here, before Global A
names an intent. Explicit selections never reach the router: a HUD command directive, a
``/skill`` selection, a plugin selection and an explicit learning command keep their own
deterministic lanes. The router answers only when it is sure; otherwise Global A's reasoning
model chooses the route from the same message, the previous assistant action and the
router's scores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from global_loop.intent.router import ROUTES, RouteDecision

ROUTE_INTENTS: Mapping[str, str] = {
    "FREEFORM_CHAT": "RESPOND",
    "CODING_AGENT": "RESPOND",
    "BROWSER_AGENT": "BROWSE",
    "DOCUMENT_CREATION": "DOCUMENT_CREATE",
}
# The route's episode lane: Omega's foldback lane and the operator_route Meta A and AUTO
# read for lane-specific mechanics. Document creation lives in the chat lane.
ROUTE_LANES: Mapping[str, str] = {
    "FREEFORM_CHAT": "chat",
    "CODING_AGENT": "agent",
    "BROWSER_AGENT": "browser",
    "DOCUMENT_CREATION": "chat",
}
_EXPLICIT_SPECIALIST_ROUTES: Mapping[str, str] = {
    "BROWSE": "BROWSER_AGENT",
    "USE_SKILL": "CODING_AGENT",
}
_ROUTED_OPENED_BY = frozenset({"operator_query", "operator_command"})
_REASON_LIMIT = 300


@dataclass(frozen=True)
class OperatorRoute:
    route: str
    source: str  # "router" | "escalation" | "operator"
    reason: str
    context: str
    decision: RouteDecision
    intent_override: str = ""

    def __post_init__(self) -> None:
        if self.route not in ROUTES:
            raise ValueError(f"unknown route {self.route!r}")
        if self.source not in {"router", "escalation", "operator", "handoff"}:
            raise ValueError(f"unknown route source {self.source!r}")

    @property
    def intent(self) -> str:
        return self.intent_override or ROUTE_INTENTS[self.route]

    @property
    def lane(self) -> str:
        return ROUTE_LANES[self.route]

    def to_dict(self) -> dict[str, Any]:
        decision = self.decision
        return {
            "route": self.route,
            "source": self.source,
            "reason": self.reason,
            "context": self.context,
            "router": {
                "route": decision.route,
                "reason": decision.reason,
                "confidence": round(decision.confidence, 6),
                "margin": round(decision.margin, 6),
                "primary": {k: round(v, 6) for k, v in decision.primary.items()},
                "intents": list(decision.intents),
                "certainty": {k: round(v, 6) for k, v in decision.certainty.items()},
            },
        }


def _message(metadata: Mapping[str, Any]) -> str:
    return str(metadata.get("message") or metadata.get("text") or "").strip()


def routes_operator_turn(ignition) -> bool:
    """True for an operator chat turn whose route no explicit selection already names."""
    dom = ignition.dominant_contributor
    if str(getattr(dom, "source", "") or "") != "operator":
        return False
    metadata = dict(dom.metadata or {})
    agent_answer = (ignition.opened_by == "operator_response"
                    and metadata.get("operator_route_pin") == "CODING_AGENT"
                    and metadata.get("agent_question_answer") and metadata.get("system_query_id"))
    if ignition.opened_by not in _ROUTED_OPENED_BY and not agent_answer:
        return False
    if not _message(metadata):
        return False
    directive = str(metadata.get("directive_intent") or "").strip().upper()
    if directive not in {"", "RESPOND", *_EXPLICIT_SPECIALIST_ROUTES}:
        return False
    if metadata.get("plugin_operation") is not None:
        return False
    from pressure_loop.contributors import infer_operator_command_kind

    command_kind = str(metadata.get("operator_command_kind") or infer_operator_command_kind(_message(metadata)) or "")
    return command_kind != "learn"


def route_from_specialist_handoff(ignition) -> OperatorRoute | None:
    """Rehydrate the host's already-classified route for a specialist lane item."""
    contributor = getattr(ignition, "dominant_contributor", None)
    metadata = dict(getattr(contributor, "metadata", None) or {})
    if (
        str(getattr(contributor, "source", "") or "") != "omega"
        or str(getattr(contributor, "type", "") or "") != "lane_work_dispatch"
        or metadata.get("lane_work_handoff") is not True
    ):
        return None
    lane = str(metadata.get("lane") or "")
    route = {"browser": "BROWSER_AGENT", "agent": "CODING_AGENT"}.get(lane)
    selected_skill = metadata.get("selected_skill")
    if selected_skill is not None and (
        type(selected_skill) is not str or not selected_skill.strip() or len(selected_skill) > 128
    ):
        raise ValueError("specialist handoff selected skill is invalid")
    directive = {
        "browser": "BROWSE",
        "agent": "USE_SKILL" if selected_skill is not None else "RESPOND",
    }.get(lane)
    if route is None or metadata.get("operator_route") != lane or metadata.get("directive_intent") != directive:
        raise ValueError("specialist handoff route does not match its dedicated lane")
    request = _message(metadata)
    if not request or not str(metadata.get("original_cycle_id") or "").strip():
        raise ValueError("specialist handoff is missing its originating chat request")
    decision = RouteDecision(
        route=route,
        reason="durable_handoff",
        confidence=1.0,
        margin=1.0,
        primary={route: 1.0},
        intents=(route,),
        intent_scores={route: 1.0},
        certainty={"known": 1.0, "ambiguous": 0.0, "out_of_domain": 0.0},
    )
    return OperatorRoute(
        route=route,
        source="handoff",
        reason="Global A already classified this durable lane handoff",
        context=str(metadata.get("route_context") or ""),
        decision=decision,
        intent_override=directive if directive == "USE_SKILL" else "",
    )


def _escalate(episode, message: str, context: str, decision: RouteDecision) -> tuple[str, str]:
    from core.episode_temporal import prepend_time_line
    from organ.promptorgan import PromptContext, phrase
    from runtime.socket import invoke_runtime_generate, pipeline_context_for_episode
    from schema.browser_perception import strict_keys

    request = phrase(PromptContext(kind="intent_escalation", question=message, data={
        "previous_action": context,
        "reason": decision.reason,
        "primary": dict(decision.primary),
        "intents": list(decision.intents),
        "certainty": dict(decision.certainty),
    }))
    response = invoke_runtime_generate(
        operation="intent_escalation", episode_id=str(episode.episode_id),
        prompt=request.user,
        system=prepend_time_line(request.system, dict(getattr(episode, "temporal", None) or {})),
        response_format=dict(request.response_format), tools=None,
        enable_thinking=False, max_tokens=512, temperature=0.0,
        **pipeline_context_for_episode(episode),
    )
    if not isinstance(response, dict) or type(response.get("text")) is not str:
        raise ValueError("intent escalation returned no text")
    payload = strict_keys(json.loads(response["text"]), {"route", "reason"}, "intent escalation output")
    route = payload["route"]
    if route not in ROUTES:
        raise ValueError(f"intent escalation chose unknown route {route!r}")
    reason = payload["reason"]
    if type(reason) is not str or len(reason) > _REASON_LIMIT:
        raise ValueError(f"intent escalation reason must be text of at most {_REASON_LIMIT} characters")
    return route, reason.strip()


def resolve_operator_route(episode, ignition) -> OperatorRoute:
    """Route one operator turn: the router's confident decision, or Global A's reasoned one."""
    from global_loop.intent.context import held_instruction_context, previous_action
    from global_loop.intent.router import get_router

    metadata = dict(ignition.dominant_contributor.metadata or {})
    message = _message(metadata)
    router_context = context = previous_action()
    explicit_directive = str(metadata.get("directive_intent") or "").strip().upper()
    explicit_route = _EXPLICIT_SPECIALIST_ROUTES.get(explicit_directive)
    if explicit_route is not None:
        decision = RouteDecision(
            route=explicit_route,
            reason="explicit_operator_directive",
            confidence=1.0,
            margin=1.0,
            primary={explicit_route: 1.0},
            intents=(explicit_route,),
            intent_scores={explicit_route: 1.0},
            certainty={"known": 1.0, "ambiguous": 0.0, "out_of_domain": 0.0},
        )
        return OperatorRoute(
            explicit_route, "operator", "explicit directive selects the specialist lane",
            context, decision,
        )
    held = metadata.get("held_instruction")
    if isinstance(held, dict) and str(held.get("job_id") or ""):
        # Words that outlived their build (T7): the build is the action they follow, and
        # cognition reads how it settled through the same context.
        router_context, context = held_instruction_context(held)
    pinned = str(metadata.get("operator_route_pin") or "").strip().upper()
    if pinned in ROUTES:
        # The host already knows the lane: an answer to the agent's own question resumes that
        # task, and a model reading the answer's words ("option 2") would route it as chat.
        return OperatorRoute(pinned, "operator", "route carried by the turn", context,
                             RouteDecision(pinned, "explicit", 1.0, 1.0, {}, (), {}, {}))
    decision = get_router().route(message, router_context)
    if not decision.escalated:
        return OperatorRoute(decision.route, "router", "accepted", context, decision)
    route, reason = _escalate(episode, message, context, decision)
    return OperatorRoute(route, "escalation", reason, context, decision)


__all__ = [
    "OperatorRoute", "ROUTE_INTENTS", "ROUTE_LANES", "resolve_operator_route",
    "route_from_specialist_handoff", "routes_operator_turn",
]
