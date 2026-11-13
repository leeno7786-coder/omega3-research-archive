"""Pure sequencer for Meta A and Meta B phase chains."""

from __future__ import annotations

from typing import Any, Dict, Optional

from guardian.terminal_feedback import emit_data, phase_span

from meta_loop import action as action_phase
from meta_loop import consistency as consistency_phase
from meta_loop import disposition as disposition_phase
from meta_loop import intake as intake_phase
from meta_loop import memory as memory_phase
from meta_loop import pack as pack_phase
from meta_loop import reflect as reflect_phase
from meta_loop import tool as tool_phase
from meta_loop.pack.run import execution_action_pack_to_dict
from meta_loop.reflect.run import meta_reflection_pack_to_dict


def _ep_id(episode) -> str:
    return str(getattr(episode, "episode_id", "") or "no-episode")


def _meta_domain(state) -> str:
    inputs = dict(getattr(state, "execution_inputs", None) or {})
    dom = inputs.get("domain") or inputs.get("subdomain")
    if dom:
        return str(dom)
    meta = dict(getattr(state, "meta", None) or {})
    for key in ("domain", "subdomain", "topic"):
        val = meta.get(key)
        if val:
            return str(val)
    return ""


def run_meta_a(episode) -> Optional[Dict[str, Any]]:
    """intake → action → tool → memory → pack; write execution_action_pack."""
    ep_id = _ep_id(episode)
    with phase_span("meta", "intake", episode_id=ep_id):
        mi = intake_phase.run(episode)
    if mi is None:
        return None
    with phase_span("meta", "action", episode_id=ep_id) as span:
        state = action_phase.run(mi)
        span.set(
            action=state.action,
            execution_intent=(state.execution_intent or "")[:150],
        )
    with phase_span("meta", "tool", episode_id=ep_id) as span:
        state = tool_phase.run(state)
        domain = _meta_domain(state)
        fields: dict[str, object] = {}
        if state.tool:
            fields["tool"] = state.tool
        if state.tool_reason:
            fields["tool_reason"] = (state.tool_reason or "")[:150]
        if domain:
            fields["domain"] = domain
        if fields:
            span.set(**fields)
    with phase_span("meta", "memory", episode_id=ep_id) as span:
        state = memory_phase.run(state)
        span.set(
            memory_required=state.memory_required,
            memory_reason=(state.memory_reason or "")[:100],
            memory_scope=list(state.memory_scope or ()),
            memory_tools_count=len(state.memory_tools or ()),
        )
    with phase_span("meta", "pack", episode_id=ep_id):
        if state.intent == "BROWSE":
            from meta_loop.browser.perception import run as perceive_browser

            with phase_span("meta", "browser_perception", episode_id=ep_id):
                state = perceive_browser(state, episode)
        pack = pack_phase.run(state)
    pack_dict = execution_action_pack_to_dict(pack)
    episode.meta = dict(getattr(episode, "meta", None) or {})
    episode.meta["execution_action_pack"] = pack_dict
    # Phase 6 (flag-gated): a USE_SKILL action selects the ExecutionSkill + builds the
    # SkillExecutionPack here in Meta A (never executes). Inert while OMEGA_USE_SKILL_ROUTING
    # is OFF — Global never emits USE_SKILL, so action is never USE_SKILL.
    skill_motor = (
        pack_dict.get("action") == "EXECUTE_MOTOR_PROGRAM"
        and (pack_dict.get("execution_inputs") or {}).get("motor_transport") == "USE_SKILL"
    )
    if pack_dict.get("action") == "USE_SKILL" or skill_motor:
        from meta_loop.skill.run import attach_skill_execution_pack

        with phase_span("meta", "skill_selection", episode_id=ep_id) as span:
            sp_dict = attach_skill_execution_pack(episode, pack_dict)
            if sp_dict:
                sel = dict(sp_dict.get("selected_skill") or {})
                span.set(
                    skill_id=sel.get("skill_id"),
                    skill_name=sel.get("skill_name"),
                    execution_kind=sel.get("execution_kind"),
                )
    try:
        emit_data(
            "meta",
            "execution_action_pack",
            episode_id=ep_id,
            summary=f"action={pack_dict.get('action')}",
            action=pack_dict.get("action"),
            tool=pack_dict.get("tool"),
            inputs=pack_dict.get("execution_inputs"),
            memory_required=bool(pack_dict.get("memory_required")),
        )
    except Exception:
        pass
    return pack_dict


def run_meta_b(episode):
    """consistency → disposition → reflect; write reflection; hand up to Global B."""
    from global_loop.loop import run_global_b

    ep_id = _ep_id(episode)
    with phase_span("meta", "consistency", episode_id=ep_id):
        mbi = consistency_phase.meta_b_input_from_episode(episode)
    if mbi is None:
        return None

    with phase_span("meta", "disposition", episode_id=ep_id) as span:
        consistency, critical = consistency_phase.run(mbi)
        disposition, status = disposition_phase.run(mbi, critical)
        span.set(
            consistency=str(consistency)[:100],
            critical=bool(critical),
            disposition=str(disposition)[:100],
            status=str(status)[:100],
        )
    with phase_span("meta", "reflect", episode_id=ep_id):
        reflection = reflect_phase.run(mbi, consistency, disposition, status)
    meta = dict(getattr(episode, "meta", None) or {})
    reflection_dict = meta_reflection_pack_to_dict(reflection)
    meta["reflection"] = reflection_dict
    episode.meta = meta
    try:
        emit_data(
            "meta",
            "reflection",
            episode_id=ep_id,
            summary=str(reflection_dict.get("execution", {}).get("disposition") or ""),
            consistency=reflection_dict.get("consistency"),
            disposition=reflection_dict.get("execution", {}).get("disposition"),
            status=reflection_dict.get("execution", {}).get("status"),
        )
    except Exception:
        pass
    return run_global_b(episode, reflection_dict)
