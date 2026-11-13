"""Global A phase — map opened_by + dominant message → ALLOWED_INTENT."""

from __future__ import annotations

from typing import Any, Mapping

from pressure_loop.contributors import (
    ALLOWED_INTENTS,
    EpisodeIgnition,
    infer_operator_command_kind,
)


# Phase 6E — the canonical skill-mediated agentic intent. When ON, Global A emits
# USE_SKILL for agentic operator work instead of USE_TOOL; Meta A selects the exact
# ExecutionSkill (6F) and Auto dispatches qwen inside it (6H). Global still NEVER names
# a skill or tool — it only chooses the skill-mediated route.
# Default resolved from config/skill_execution.yaml; env OMEGA_USE_SKILL_ROUTING overrides.


def _routing_default_from_config() -> bool:
    try:
        from core.skill_execution_config import _cfg

        routing = _cfg().get("routing") or {}
        if isinstance(routing, dict) and "use_skill_default" in routing:
            return bool(routing.get("use_skill_default"))
    except Exception:
        pass
    return True


OMEGA_USE_SKILL_ROUTING = _routing_default_from_config()


def use_skill_routing_enabled() -> bool:
    """Resolve the skill-routing flag — env OMEGA_USE_SKILL_ROUTING overrides the module
    constant (which reflects config/skill_execution.yaml by default)."""
    import os

    env = os.environ.get("OMEGA_USE_SKILL_ROUTING")
    if env is not None and env.strip() != "":
        return env.strip().lower() in ("1", "true", "yes", "on")
    return OMEGA_USE_SKILL_ROUTING


def _agentic_intent() -> str:
    return "USE_SKILL" if use_skill_routing_enabled() else "USE_TOOL"


def _canonical_intent(intent: str) -> str:
    """Normalize legacy intents at Global A emit time. APPLY is retired: when skill routing
    is ON, map it to USE_SKILL so Omega-prepared APPLY decisions reach the skill path."""
    i = str(intent or "").strip().upper()
    if i == "APPLY" and use_skill_routing_enabled():
        return "USE_SKILL"
    return i


def _guard_use_skill(intent: str, ignition: EpisodeIgnition) -> str:
    """CHOKEPOINT (operator + Omega 2026-07-08): USE_SKILL is reachable ONLY
    with a real objective — every objectiveless dispatch dies as
    missing_skill_pack, and three separate roads found that cliff (the
    execution_gap loop, the queued-input era, episode 75dbdbd9's
    mind-prepared APPLY). Operator turns always pass (their message IS the
    objective). A stashed build/practice objective passes. Otherwise the
    mind's APPLY gets one honest chance to MINT its practice objective on
    the spot (part-learned + warm substrate -> a real skill pack and build
    command); if nothing is practiceable yet, downgrade to IDLE —
    never a doomed dispatch. A mint operation that raises is a Global A failure, not
    evidence that no objective exists; let the phase boundary record and relay it."""
    if intent != "USE_SKILL":
        return intent
    dom = getattr(ignition, "dominant_contributor", None)
    if (
        str(getattr(dom, "source", "") or "") == "operator"
        or str(getattr(ignition, "opened_by", "") or "") in _OPERATOR_SOURCE_OPENED_BY
    ):
        return intent
    from memory.build_objective import current_build_objective

    if current_build_objective() is not None:
        return intent
    if str(getattr(ignition, "opened_by", "") or "") == "application_gap":
        from pressure_loop.practice_mint import ensure_practice_objective

        if ensure_practice_objective():
            return intent
    from pressure_loop.practice_mint import mint_for_prepared_apply

    if mint_for_prepared_apply():
        return intent  # a real objective now backs the dispatch
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "global",
            "use_skill_undispatchable",
            status="skip",
            summary="USE_SKILL without objective -> IDLE",
            opened_by=str(getattr(ignition, "opened_by", "") or ""),
        )
    except Exception:
        pass
    return "IDLE"


def _dominant_message(ignition: EpisodeIgnition) -> str:
    dom = ignition.dominant_contributor
    return str(dom.metadata.get("message") or dom.metadata.get("text") or "")


def _has_valid_omega_emergent_pressure(source: str, metadata: Mapping[str, Any] | None) -> bool:
    meta = dict(metadata or {})
    if source != "omega" or "emergent_pressure" not in meta:
        return False
    payload = meta["emergent_pressure"]
    if not isinstance(payload, dict):
        raise ValueError("emergent_pressure must be a dictionary")
    from schema.emergent_pressure import EmergentPressure

    EmergentPressure.from_dict(payload)
    return True


def _map_opened_by_to_intent(
    opened_by: str,
    message: str,
    *,
    source: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> str:
    ob = (opened_by or "").strip()
    operation = (metadata or {}).get("plugin_operation")
    selector_only = isinstance(operation, Mapping) and set(operation) == {"plugin_id"}
    explicit_plugin = not selector_only and bool(
        operation or (metadata or {}).get("directive_intent") == "PLUGIN_OPERATION"
    )
    if _has_valid_omega_emergent_pressure(source, metadata):
        return "PURSUE_OUTCOME"
    if ob == "operator_query":
        # The AUTO model discovers and calls connected apps on the same native
        # tool transcript as every other action. Word overlap is not authority
        # to select a provider or remove the model from its own result loop.
        return "PLUGIN_OPERATION" if explicit_plugin else "RESPOND"
    if ob == "operator_response":
        return "RESOLVE_QUERY"
    if ob == "proactive_update":
        return "PROACTIVE_RESPONSE"
    if ob == "operator_priority":
        # The mind's own ask (Omega pressure_feedback, source "omega") WON the
        # ignition contest — the system-generated query is a FIRST-CLASS
        # artifact with its own route (never PROACTIVE_RESPONSE: clean
        # separation, operator directive 2026-06-12). One-shot semantics live
        # at the pressure level: a losing ask evaporates.
        return "ASK_OPERATOR"
    if ob in ("learning_debt", "curiosity_gap", "epistemic_emptiness", "mastery_gap"):
        # mastery_gap is the MIND-PRIMACY learning signal — the sole autonomous learning
        # contributor once the deterministic floor (learning_debt) is suppressed: emitted
        # by the Omega lever (mastery_debt_delta -> mastery_gap), the P5 sustain, AND the
        # deepsleep reseed. It MUST route to LEARN (not the IDLE catch-all) or the
        # mind-driven learn loop never closes — knowledge never rises.
        return "LEARN"
    if ob == "clarification_gap":
        # The mind's own ASK-OPERATOR lever (clarification_debt_delta -> clarification_gap):
        # a mind-raised "I genuinely need the operator" that ignites a real ASK_OPERATOR
        # episode (which authors the question -> system_messages -> HUD). The mirror of the
        # mastery_gap learning lever and the execution_gap build lever (operator 2026-06-27).
        return "ASK_OPERATOR"
    if ob in ("identity_gap", "trajectory_gap"):
        return "IDLE"
    if ob in ("execution_gap", "application_gap", "execution_continuation"):
        # Agentic execution (learned-but-unapplied + skill continuation) routes through the SKILL
        # layer — USE_SKILL has RETIRED the legacy APPLY action. The kill-switch fallback is
        # USE_TOOL (the model responds without a qwen dispatch); APPLY no longer exists.
        return "USE_SKILL" if use_skill_routing_enabled() else "USE_TOOL"
    # Phase 7M — taxonomy advisory pressure kinds (when memory_taxonomy wins ignition).
    # Mirrors config/global_intent.yaml opened_by_map. Agentic repair/verify -> USE_SKILL;
    # safety blocks -> ASK_OPERATOR; low-confidence -> LEARN.
    if ob in (
        "capability_gap",
        "repair_needed",
        "tool_failure",
        "verification_needed",
        "incomplete_task",
    ):
        return _agentic_intent()
    if ob == "confidence_reinforcement":
        return "LEARN"
    if ob == "requires_operator_input":
        return "ASK_OPERATOR"
    # Memory Engine v2 recall-verdict pressure (always-on recall_verdict contributor).
    # memory_gap -> fill a verified recall hole; sense/conflict -> metacognitive reconcile.
    if ob == "memory_gap":
        return "LEARN"
    if ob in ("sense_resolution", "memory_reconciliation"):
        return "IDLE"
    if ob == "self_wake":
        # Idle-wake governor (operator 2026-07-08): the self-initiated
        # mastery review — Omega looks at knowledge vs application against
        # targets and sets its own pressure for what to learn or apply.
        return "IDLE"
    if ob == "operator_command":
        # An operator command is conversation with a job in it (operator
        # 2026-09-05: "one pipeline for chat ... it will chat, use tools, search
        # the web etc on command"). Only two things leave the RESPOND rail: an
        # explicit learning ask (LEARN) and a structured plugin directive.
        # Everything else — build/fix/run/inspect/plugin requests included —
        # is answered by the chat brain, which calls its bounded tools on demand
        # inside AUTO's RESPONSE action. The verb regex that used to send these
        # turns to USE_SKILL (Meta A skill selection + Omega motor planning
        # before any model read the words) is retired; that rail now carries
        # machine-minted work only. The ingress-stamped kind survives FIFO
        # delay/restart; legacy queued records are classified here by the same
        # deterministic rule.
        command_kind = str((metadata or {}).get("operator_command_kind") or "")
        if not command_kind:
            command_kind = str(infer_operator_command_kind(message) or "")
        if command_kind == "learn":
            return "LEARN"
        if command_kind == "remember":
            # Explicit memory intake is conversational: Guardian has already
            # admitted the external statement, episode SRAM holds it, and
            # DeepSleep commits it after the response.
            return "RESPOND"
        return "PLUGIN_OPERATION" if explicit_plugin else "RESPOND"
    if ob in ("idle_probe_aggregate",):
        return "IDLE"
    return "IDLE"


# Genuine operator-INPUT sources: an ignition opened by these is an operator turn.
_OPERATOR_SOURCE_OPENED_BY = frozenset({"operator_query", "operator_command", "operator_response"})


def run(ignition: EpisodeIgnition, route: Any = None) -> str:
    # An operator chat turn with no explicit selection was routed by Global A's
    # intent router (or its reasoning model) before this phase; the route names
    # the episode's intent. Explicit UI directives, /skill and plugin selections
    # and learning commands never reach the router and resolve below, verbatim.
    if route is not None:
        return route.intent
    dom = ignition.dominant_contributor
    metadata = dict(dom.metadata or {})
    source = str(getattr(dom, "source", "") or "")
    kind = str(getattr(dom, "type", "") or "")
    if "omega_control" in metadata or kind == "omega_successor":
        from pressure_loop.omega_successor import validate_successor
        if source != "omega" or kind != "omega_successor":
            raise ValueError("successor requires Omega provenance")
        intent = validate_successor(metadata).pressure.intent
        # Operator work carries its own objective; an idle-learning build still needs its
        # practice objective minted before it can dispatch.
        return intent if metadata.get("original_cycle_id") else _guard_use_skill(intent, ignition)
    # Routing metadata describes the action, never a physical FIFO worker.
    from pressure_loop.window_lane import WINDOW_REFLECTION_READY

    if source == "omega" and kind == WINDOW_REFLECTION_READY:
        return "REFLECT_WINDOW"
    omega_emergent_pressure = _has_valid_omega_emergent_pressure(source, metadata)
    directive = str(metadata.get("directive_intent") or "").strip().upper()
    operation = metadata.get("plugin_operation")
    if directive == "PLUGIN_OPERATION" and isinstance(operation, Mapping) and set(operation) == {"plugin_id"}:
        # An @app attachment limits the native model's catalog. It selects no
        # capability or arguments and must not start the old objective planner.
        directive = "RESPOND"
    if directive == "APPLY":
        return _canonical_intent("APPLY")
    if directive in ALLOWED_INTENTS and directive != "PURSUE_OUTCOME":
        # Ordinary chat stamps RESPOND at ingress. The native model owns
        # connected-app discovery and invocation on that same response rail.
        # Inferred operator commands likewise carry ``operator_command_kind``;
        # their USE_SKILL stamp owns the lane, but not the provider decision.
        # Structured plugin/permission directives keep their explicit action.
        # Explicit UI directives have no inferred command kind and remain
        # authoritative.
        inferred_operator_command = (
            ignition.opened_by == "operator_command"
            and type(metadata.get("operator_command_kind")) is str
            and bool(str(metadata["operator_command_kind"]).strip())
        )
        if not (
            (directive == "RESPOND" and ignition.opened_by == "operator_query")
            or inferred_operator_command
        ):
            return _canonical_intent(directive)

    message = _dominant_message(ignition)
    det = (
        "PURSUE_OUTCOME"
        if omega_emergent_pressure
        else _map_opened_by_to_intent(
            ignition.opened_by,
            message,
            source=source,
            metadata=metadata,
        )
    )

    if det not in ALLOWED_INTENTS:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "global",
                "intent_idle_fallback",
                status="skip",
                summary=f"fallback={det}->IDLE",
                original=det,
                fallback="IDLE",
                opened_by=str(getattr(ignition, "opened_by", "") or ""),
            )
        except Exception:
            pass
        det = "IDLE"
    return _guard_use_skill(_canonical_intent(det), ignition)
