"""Pure sequencer for Global A and Global B phase chains."""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional

from guardian.terminal_feedback import emit_data, phase_span
from pressure_loop.contributors import EpisodeIgnition, IntentDecision

from global_loop import (
    assemble as assemble_phase,
    followups as followups_phase,
    ground as ground_phase,
    ignition as ignition_phase,
    intent as intent_phase,
    package,
    summarize as summarize_phase,
)

log = logging.getLogger("omega.global_loop")


def _ep_id(episode) -> str:
    return str(getattr(episode, "episode_id", "") or "no-episode")


_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> set:
    return {t for t in _TOKEN_RE.split(str(text or "").lower()) if len(t) > 2}


def _trajectory_alignment(domain: str, intent: str, snapshot) -> tuple:
    """Deterministic intent/domain-vs-trajectory alignment — no goals, just how the
    episode sits against the active trajectory. Returns (label, divergence 0..1).

    Subject is the RESOLVED topic domain only. Non-learning episodes with an empty
    domain are not trajectory drift — never fall back to the raw intent word.
    """
    snap = snapshot or {}
    target_domain = str(snap.get("target_domain") or "").strip()
    target_cap = str(snap.get("target_capability") or "").strip()
    if not target_domain and not target_cap:
        return "no-trajectory", 0.0
    if not (domain and domain.strip()):
        return "no-trajectory", 0.0
    subject = domain.strip()
    ep_tokens = _tokens(subject)
    target_tokens = _tokens(f"{target_domain} {target_cap}")
    if not ep_tokens or not target_tokens:
        return "divergent", 1.0
    jaccard = len(ep_tokens & target_tokens) / len(ep_tokens | target_tokens)
    score = round(1.0 - jaccard, 3)
    return ("on-trajectory" if score <= 0.5 else "divergent"), score


def run_global_a(episode, ignition: EpisodeIgnition) -> Optional[IntentDecision]:
    """ignition → intent; write intent_decision on episode (no goal, no target)."""
    ep_id = _ep_id(episode)
    # Fail-hard cognition (operator directive: cognition crashes the system). A
    # compute failure in any Global A phase propagates and HALTS the cascade —
    # it is never swallowed to None. phase_span emits "fail" first.
    with phase_span("global", "ignition", episode_id=ep_id):
        ign = ignition_phase.run(ignition)
    from global_loop.intent.route import (
        resolve_operator_route,
        route_from_specialist_handoff,
        routes_operator_turn,
    )

    operator_route = None
    operator_turn = routes_operator_turn(ign)
    operator_episode = str(getattr(ign.dominant_contributor, "source", "") or "") == "operator"
    if operator_turn:
        with phase_span("global", "route", episode_id=ep_id) as span:
            operator_route = resolve_operator_route(episode, ign)
            span.set(route=operator_route.route, source=operator_route.source,
                     router_reason=operator_route.decision.reason,
                     confidence=round(operator_route.decision.confidence, 4))
    else:
        with phase_span("global", "route", episode_id=ep_id) as span:
            operator_route = route_from_specialist_handoff(ign)
            if operator_route is not None:
                span.set(route=operator_route.route, source=operator_route.source,
                         router_reason=operator_route.decision.reason,
                         confidence=round(operator_route.decision.confidence, 4))
    with phase_span("global", "intent", episode_id=ep_id) as span:
        mapped_intent = intent_phase.run(ign, route=operator_route)
        span.set(intent=mapped_intent, opened_by=ignition.opened_by)

    dom_weight = ign.dominant_contributor.weight
    confidence = max(0.0, min(1.0, float(dom_weight or ign.aggregate_pressure)))
    reason = f"opened_by={ign.opened_by} aggregate={ign.aggregate_pressure:.3f}"
    from schema.omega_control_decision import omega_lane_for_intent

    # Global A classifies an operator turn while it is still inside the permanent
    # Chat lane. A specialist route is a destination for the durable handoff, not
    # permission for this Chat episode to wear the target lane identity (which would
    # let its Global B claim and consume the specialist's addressed mail).
    lane = (
        "chat" if operator_episode else
        operator_route.lane if operator_route is not None else
        omega_lane_for_intent(mapped_intent)
    )
    decision = IntentDecision(
        episode_id=ign.episode_id,
        intent=mapped_intent,
        confidence=confidence,
        reason=reason,
        omega_lane=lane,
    )

    episode.global_ = dict(getattr(episode, "global_", None) or {})
    episode.global_["intent_decision"] = {
        "episode_id": decision.episode_id,
        "intent": decision.intent,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "omega_lane": decision.omega_lane,
    }
    if operator_route is not None:
        metadata = ign.dominant_contributor.metadata
        episode.global_["operator_route"] = operator_route.to_dict() | {
            "lane": operator_route.lane,
            "request": str(metadata.get("message") or metadata.get("text") or "")}
    if mapped_intent == "BROWSE":
        from global_loop.browser_workflow import bind_browser_workflow

        episode.global_["browser_workflow"] = bind_browser_workflow(
            ign,
            operator_turn=operator_episode,
        )
    try:
        emit_data(
            "global",
            "intent_decision",
            episode_id=ep_id,
            summary=f"intent={decision.intent}",
            intent=decision.intent,
            confidence=decision.confidence,
            reason=decision.reason,
        )
    except Exception:
        pass
    return decision


def run_global_b(
    episode,
    meta_reflection: Mapping[str, Any],
) -> Optional[package.GlobalOutcomePack]:
    """Meta B reflection → assemble → episode outcome (intent × trajectory ×
    execution) → ground → summarize → followups → package. No goal, no goal_state."""
    ep_id = _ep_id(episode)
    # Fail-hard cognition (operator directive: cognition crashes the system). A
    # compute failure in any Global B phase propagates and HALTS — never
    # swallowed to None. The ONLY legitimate None is "nothing to assemble"
    # (no folded AUTO outcome), preserved below. phase_span emits "fail" first.
    with phase_span("global", "assemble", episode_id=ep_id):
        gbi = assemble_phase.run(episode, meta_reflection)
    if gbi is None:
        return None

    exec_block = dict(meta_reflection.get("execution") or {})
    disposition = str(exec_block.get("disposition") or "invalid")

    auto_inner_dict = assemble_phase.auto_inner(gbi.auto_outcome)
    action = str(gbi.execution_pack.get("action") or exec_block.get("action") or "")
    failure_kind = auto_inner_dict.get("failure_kind")

    # Episode outcome: the intent chosen this episode, how it sits against the
    # active trajectory, and Meta B's execution verdict (success/failure + reason).
    with phase_span("global", "episode_outcome", episode_id=ep_id) as span:
        intent = str(gbi.source_action_goal.get("intent") or "")
        domain = str((gbi.execution_pack.get("execution_inputs") or {}).get("domain") or "")
        alignment, divergence = _trajectory_alignment(domain, intent, gbi.trajectory_snapshot)

        mb_result = str(exec_block.get("result") or "none")
        exec_status = mb_result if mb_result in ("success", "failure", "partial") else "none"
        execution_outcome = {
            "executed": bool(exec_block.get("executed")),
            "status": exec_status,
            "failure_kind": exec_block.get("failure_kind"),
            "failure_reason": exec_block.get("failure_reason")
            if exec_status == "failure"
            else None,
        }
        # compat status for legacy window/deepsleep readers (== old goal_state vocab)
        compat_status = disposition
        span.set(
            intent=intent,
            disposition=disposition,
            trajectory_alignment=alignment,
            execution_status=exec_status,
        )

    with phase_span("global", "ground", episode_id=ep_id):
        grounded, grounding_basis = ground_phase.run(gbi.auto_outcome, meta_reflection)
    with phase_span("global", "summarize", episode_id=ep_id):
        summary = summarize_phase.run(compat_status, action, failure_kind)
    with phase_span("global", "followups", episode_id=ep_id):
        followup_list = followups_phase.run(compat_status, failure_kind, action)

    with phase_span("global", "package", episode_id=ep_id):
        episode_temporal = getattr(episode, "temporal", None) or {}
        episode_learning_state = getattr(episode, "learning_state", None) or {}
        pack = package.run(
            gbi,
            intent=intent,
            trajectory_alignment=alignment,
            divergence_score=divergence,
            execution_outcome=execution_outcome,
            compat_status=compat_status,
            grounded=grounded,
            grounding_basis=grounding_basis,
            summary=summary,
            followups=followup_list,
            episode_temporal=episode_temporal,
            episode_learning_state=episode_learning_state,
        )
        package.write_outcome(episode, pack)
        from omega_loop.episode.packet import attach_omega_packet

        attach_omega_packet(episode, gbi, meta_reflection)
        _bind_admitted_mail(episode)
    return pack


def _bind_admitted_mail(episode) -> None:
    """Record in the derived pack exactly which cross-lane mail the Omega fold was admitted.

    The mail is bound inside ``attach_omega_packet``, after the pack is written, so the
    references are added here. They are the admitted identities (mail id and generation) from
    the binding record; lease internals stay host bookkeeping. No binding record at all is
    missing context and says why, distinct from a binding that decided on no mail.
    """
    global_ = dict(getattr(episode, "global_", None) or {})
    outcome = global_.get("outcome")
    if not isinstance(outcome, dict):
        return
    record = global_.get("omega_mail")
    if isinstance(record, Mapping):
        refs = {
            "disposition": str(record.get("disposition") or ""),
            "mail_refs": [
                {"mail_id": str(row.get("mail_id") or ""), "generation": row.get("generation")}
                for row in (record.get("receipts") or ())
                if isinstance(row, Mapping)
            ],
            "cutoff_seq": record.get("cutoff_seq"),
            "omitted_required_mail_ids": [
                str(one) for one in (record.get("omitted_required_mail_ids") or ()) if str(one)
            ],
            "reason": str(record.get("error") or ""),
        }
    else:
        refs = {
            "disposition": "not_recorded",
            "mail_refs": [],
            "cutoff_seq": None,
            "omitted_required_mail_ids": [],
            "reason": "no cross-lane mail binding was recorded for this fold (it carries no "
            "thread or episode identity to bind under)",
        }
    global_["outcome"] = {**outcome, "cross_lane_mail": refs}
    episode.global_ = global_
