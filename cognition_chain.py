"""Cognition chain handoff — runs the ignition cascade after the pressure loop ignites.

The pressure loop (`pressure_loop/`) owns ONLY pressure: it produces an
``EpisodeIgnition``. This module is the HANDOFF: on ignition it opens the episode
and cascades the cognition layers, each igniting the next when its pack is ready —
Global A → Meta A → AUTO → Meta B → Global B → Omega → DeepSleep. No central
shared-state/loop ties the layers; the episode is the carrier and DeepSleep owns
the per-episode close transaction through EpisodeStore.

NOTE: transitional home. This relocates as the per-layer loops/slices are moved
into their own directories.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import replace
from typing import Any, Mapping

from core import pipeline_state as _pipeline_state
from core.cognition_lock import pipeline_cognition_lock
from guardian.terminal_feedback import phase_span
from pressure_loop.policy import fetched_comprehension_timeout_seconds

log = logging.getLogger("omega.cognition_chain")


def _record_operator_ingress(store, episode, ignition) -> None:
    """Record operator sensory + brain_turn receipts on the open episode."""
    dom = getattr(ignition, "dominant_contributor", None)
    if dom is None or getattr(dom, "source", "") != "operator":
        return
    meta = dict(getattr(dom, "metadata", None) or {})
    if getattr(dom, "type", "") == "plugin_permission_decision":
        from memory import episode_sram
        from memory.api import CanonicalEnvelope
        from pressure_loop.episode import make_sensory_record
        from pressure_loop.plugin_continuations import (
            queued_plugin_permission_continuation_from_contributor,
        )

        continuation = queued_plugin_permission_continuation_from_contributor(dom)
        decision = continuation.decision
        lineage = continuation.lineage
        payload = {
            "request_id": decision.request_id,
            "expected_revision": decision.expected_revision,
            "decision": decision.decision,
            "prior_episode_id": lineage.prior_episode_id,
            "task_id": lineage.task_id,
            "plan_id": lineage.plan_id,
            "plan_digest": lineage.plan_digest,
        }
        episode_id = _episode_id(episode)
        store.append_sensory(
            make_sensory_record(
                source="user",
                kind="plugin_permission_decision",
                payload=payload,
            )
        )
        store.append_brain_turn(
            {
                "cycle_id": str(meta["cycle_id"]),
                "ingress_class": "authority_request",
                "outcome_hint": "pressure_ignited",
                "action_kind": "PLUGIN_OPERATION",
                "ingress_confidence": 1.0,
                "pressure_contribution": float(getattr(ignition, "aggregate_pressure", 0.0) or 0.0),
            }
        )
        enrichment = {
            "event_kind": "plugin_permission_decision",
            "episode_id": episode_id,
            **payload,
        }
        episode_sram.hold_memory(
            CanonicalEnvelope(
                cycle_id=episode_id,
                coord=[6, 0, 0],
                type="plugin_permission_decision",
                payload={
                    "title": "Plugin permission decision",
                    "summary": f"Plugin permission {decision.decision}",
                    "text": (
                        f"request_id={decision.request_id} decision={decision.decision} "
                        f"plan_id={lineage.plan_id}"
                    ),
                    "subtopic": "plugin_permission_decision",
                    "tags": ["plugin", "permission", "continuation"],
                    "enrichment": enrichment,
                },
                source="cognition_chain.plugin_permission_continuation",
                confidence=1.0,
                provenance=["operator_queue", f"prior_episode:{lineage.prior_episode_id}"],
                decay_policy="none",
                permissions={"read": True, "write": True},
                strength_index=1.0,
            ),
            propose=False,
            bypass_guardian=True,
            skip_embed=True,
        )
        return
    message = str(meta.get("message") or meta.get("text") or "").strip()
    if not message:
        return
    ingress_class = str(meta.get("ingress_class") or "operator_message")
    cycle_id = str(meta.get("cycle_id") or getattr(episode, "episode_id", "") or "")
    try:
        from pressure_loop.episode import make_sensory_record

        store.append_sensory(
            make_sensory_record(
                source="user",
                kind="operator_message",
                payload={
                    "message": message,
                    "ingress_class": ingress_class,
                    "directive_intent": meta.get("directive_intent"),
                    "cycle_id": cycle_id,
                },
            )
        )
        store.append_brain_turn(
            {
                "cycle_id": cycle_id,
                "ingress_class": ingress_class,
                "outcome_hint": "pressure_ignited",
                "action_kind": str(meta.get("directive_intent") or dom.type),
                "ingress_confidence": float(getattr(dom, "weight", 0.0) or 0.0),
                "pressure_contribution": float(getattr(ignition, "aggregate_pressure", 0.0) or 0.0),
            }
        )
    except Exception:
        log.warning("operator ingress receipt append failed", exc_info=True)


def _attach_pending_system_events(episode) -> None:
    """Drain cognition-visible system events onto the episode before Global A.

    Rollback events are facts about the current workspace state. They are queued
    outside user chat, then attached to exactly one episode so Omega can fold
    them into memory and future reasoning does not assume rolled-back files are
    still current.
    """
    try:
        from pressure_loop.system_event_queue import drain_rollback_events

        events = list(drain_rollback_events())
    except Exception:
        log.debug("system event drain failed", exc_info=True)
        return
    if not events:
        return
    meta = dict(getattr(episode, "meta", None) or {})
    existing = list(meta.get("dev_rollback_events") or [])
    seen = {str(e.get("event_id") or "") for e in existing if isinstance(e, dict)}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen:
            continue
        existing.append(dict(event))
        if event_id:
            seen.add(event_id)
    meta["dev_rollback_events"] = existing
    system_events = list(meta.get("system_events") or [])
    system_events.extend({"type": "dev_rollback", **dict(e)} for e in events)
    meta["system_events"] = system_events
    episode.meta = meta

    pressure = dict(getattr(episode, "pressure", None) or {})
    contributors = list(pressure.get("contributors") or [])
    detail = {
        "field_id": "dev_rollback",
        "message": "; ".join(str(e.get("message") or "") for e in events if isinstance(e, dict))[
            :800
        ],
        "rollback_events": [dict(e) for e in events if isinstance(e, dict)],
    }
    updated = False
    for row in contributors:
        if not isinstance(row, dict) or str(row.get("id") or "") != "dev_rollback":
            continue
        row_detail = dict(row.get("detail") or row.get("metadata") or {})
        row_detail["rollback_events"] = detail["rollback_events"]
        if detail["message"]:
            row_detail["message"] = detail["message"]
        row["detail"] = row_detail
        updated = True
        break
    if not updated:
        contributors.insert(
            0,
            {
                "id": "dev_rollback",
                "weight": 1.0,
                "detail": detail,
            },
        )
    pressure["contributors"] = contributors
    episode.pressure = pressure
    # Surface each rollback as a chat turn: the operator sees it, and the normal
    # chat intake records the resulting file state. Failure-soft.
    try:
        from memory.hot_buffer import append_turn

        for e in events:
            if not isinstance(e, dict):
                continue
            fp = str(e.get("file_path") or "")
            if not fp:
                continue
            verb = "deleted" if e.get("deleted") else "rolled back"
            append_turn(
                "assistant",
                f"You {verb} {fp} — its current state now differs from before the rollback.",
            )
    except Exception:
        log.debug("dev rollback chat surfacing failed (failure-soft)", exc_info=True)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "system_events_attached",
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            summary=f"attached {len(events)} dev rollback event(s)",
            rollback_event_count=len(events),
        )
    except Exception:
        pass


def _record_system_message(episode) -> None:
    """Land a winning ASK_OPERATOR episode's query in the text app.

    Infancy-stage proactive channel (operator spec 2026-06-12): an
    ASK_OPERATOR episode that produced ``response_text`` becomes one system
    query in ``pressure_loop/system_messages.py`` — the HUD badge panel where
    the operator responds / ignores / deletes. CLEAN ROUTE SEPARATION: the
    text app is the ASK artifact's channel ONLY — PROACTIVE_RESPONSE
    statements and operator-initiated episodes never land here.
    Failure-soft: the text app must never affect episode close.
    """
    try:
        global_ = dict(getattr(episode, "global_", None) or {})
        intent = str(
            (global_.get("intent_decision") or global_.get("action_goal") or {}).get("intent") or ""
        )
        if intent != "ASK_OPERATOR":
            return
        text = _extract_response_text(episode)
        if not text:
            return
        seed_question = ""
        pressure = dict(getattr(episode, "pressure", None) or {})
        for row in pressure.get("contributors") or []:
            if not isinstance(row, dict):
                continue
            meta = dict(row.get("metadata") or row.get("detail") or {})
            candidate = str(meta.get("question") or "").strip()
            if candidate:
                seed_question = candidate
                break
        from pressure_loop.system_messages import append_system_query

        append_system_query(
            text=str(text),
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            opened_by=str(getattr(episode, "opened_by", "") or ""),
            seed_question=seed_question,
        )
    except Exception:
        log.warning("system message record failed (failure-soft)", exc_info=True)


def _announce_agent_completion(episode, cascade_status: str) -> None:
    """Tell the operator their dispatched work ended (Phase 4 cut 9).

    A no-op for every episode that is not durable dispatched work. FAIL-HARD by
    design, unlike its neighbours: a build that ran for minutes and then ended silently is
    the precise failure mode the operator has
    already been burned by (the completion notice that never fired, audit
    2026-07-01) — and unlike the badge/pinboard writes beside it, this record IS
    the only thing that closes the loop on a request the operator was told would
    be reported back. The durable store underneath degrades softly on its own
    I/O errors, so reaching an exception here means a programming fault, which
    is exactly what fail-hard is for.
    """
    from pressure_loop.agent_lane import announce_completion

    announce_completion(episode, cascade_status=str(cascade_status or ""))


def _extract_response_text(episode) -> str:
    global_ = dict(getattr(episode, "global_", None) or {})
    auto = dict(getattr(episode, "auto", None) or {})
    text = (auto.get("outputs") or {}).get("response_text")
    if not text:
        artifacts = dict((global_.get("outcome") or {}).get("artifacts") or {})
        text = artifacts.get("response_text")
    return str(text or "").strip()


def _operator_chat_async_payload(episode, text: str) -> dict[str, Any]:
    global_ = dict(getattr(episode, "global_", None) or {})
    auto = dict(getattr(episode, "auto", None) or {})
    meta = dict(getattr(episode, "meta", None) or {})
    prompt_pack = dict(auto.get("prompt_pack") or {})
    execution_pack = dict(meta.get("execution_action_pack") or {})
    route = {
        "opened_by": str(getattr(episode, "opened_by", "") or ""),
        "intent": (global_.get("intent_decision") or global_.get("action_goal") or {}).get(
            "intent"
        ),
        "action": execution_pack.get("action"),
    }
    payload = {
        "delivery": {
            "text": str(text or ""),
            "rendered_by": "omega_async",
            "prompt_type": prompt_pack.get("prompt_type"),
            "response_mode": prompt_pack.get("response_mode"),
        },
        "route": route,
        "prompt_pack": {
            "prompt_type": prompt_pack.get("prompt_type"),
            "response_mode": prompt_pack.get("response_mode"),
            "system_prompt_hash": prompt_pack.get("system_prompt_hash"),
            "user_prompt_hash": prompt_pack.get("user_prompt_hash"),
            "remembered_knowledge_hash": prompt_pack.get("remembered_knowledge_hash"),
            "tool_block_hash": prompt_pack.get("tool_block_hash"),
        },
        "thinking": {},
    }
    try:
        from pressure_loop.operator_chat import _thinking_from_auto

        thinking = _thinking_from_auto(
            auto=auto,
            route=route,
            prompt_pack=prompt_pack,
        )
        # The async transport is a public HUD boundary; do not carry the
        # provider-private reasoning trace across it.
        thinking.pop("reasoning", None)
        payload["thinking"] = thinking
    except Exception:
        log.debug("operator chat async thinking payload unavailable", exc_info=True)
    return payload


def _record_proactive_chat(episode) -> None:
    """Land a winning PROACTIVE_RESPONSE episode in the HUD side pinboard.

    Autonomous status updates and narration while cognition runs — the right
    proactive pinboard route (``pressure_loop/proactive_chat_messages.py``). Clean
    route separation: ASK_OPERATOR questions use the text app; operator-initiated
    RESPOND episodes use ``operator_chat_responses``. Failure-soft.
    """
    try:
        global_ = dict(getattr(episode, "global_", None) or {})
        intent = str(
            (global_.get("intent_decision") or global_.get("action_goal") or {}).get("intent") or ""
        )
        if intent != "PROACTIVE_RESPONSE":
            return

        # Ensure there are no operator contributors to prevent duplicate rendering
        pressure = dict(getattr(episode, "pressure", None) or {})
        contributors = list(pressure.get("contributors") or [])
        if any(
            isinstance(row, dict)
            and str(row.get("id") or "")
            in ("operator_query", "operator_command", "operator_response")
            for row in contributors
        ):
            return

        from pressure_loop.proactive_chat_messages import append_update

        omega_result = global_.get("omega_result")
        raw_result = (
            omega_result.get("proactive_result") if isinstance(omega_result, dict) else None
        )
        if isinstance(raw_result, dict):
            # Operator directive 2026-07-12: presented results post EXACTLY like
            # system questions — into the questions panel, pending until the
            # operator responds. The RESPONSE is the teacher's grade, and only
            # the grade triggers the lifecycle fold (system_messages routes it
            # to fold_presented_result). Nothing auto-resolves at presentation;
            # the mind no longer grades its own homework.
            from pressure_loop.proactive_lifecycle import mark_result_presented
            from pressure_loop.proactive_result_renderer import render_proactive_result
            from pressure_loop.system_messages import append_result_presentation
            from schema.proactive_lifecycle import ProactiveResult

            result = ProactiveResult.from_dict(raw_result)
            text = render_proactive_result(result)
            prior = dict(global_.get("omega_packet") or {}).get("prior_mastery")
            prior_application = 0.0
            if isinstance(prior, dict):
                try:
                    prior_application = float(prior.get("application_mastery") or 0.0)
                except (TypeError, ValueError):
                    prior_application = 0.0
            mastery_delta = 0.0
            effects = omega_result.get("mastery_effects") or ()
            if effects and isinstance(effects[0], dict):
                try:
                    mastery_delta = max(
                        0.0,
                        float(effects[0].get("application_mastery") or 0.0) - prior_application,
                    )
                except (TypeError, ValueError):
                    mastery_delta = 0.0
            record = append_result_presentation(
                text=text,
                episode_id=str(getattr(episode, "episode_id", "") or ""),
                opened_by=str(getattr(episode, "opened_by", "") or ""),
                result_id=result.result_id,
                thread_id=result.thread_id,
                mastery_delta=mastery_delta,
            )
            if record is None:
                return
            mark_result_presented(result.result_id, message_id=str(record.get("id") or ""))
            return

        text = _extract_response_text(episode)
        if not text:
            return

        append_update(
            text=text,
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            opened_by=str(getattr(episode, "opened_by", "") or ""),
            intent=intent,
        )
    except Exception:
        log.debug("proactive chat record failed (failure-soft)", exc_info=True)


def _chat_correlation(episode) -> dict[str, object]:
    """Return the physical request context already bound to this episode."""
    from runtime.socket import pipeline_context_for_episode

    context = pipeline_context_for_episode(episode)
    return {
        "pipeline_id": context.get("pipeline_id"),
        "work_kind": str(context.get("work_kind") or ""),
        "job_id": str(context.get("job_id") or "") or None,
    }


# Agent and browser work may continue through Omega successors, so its answer waits for Omega
# to settle the work rather than for one AUTO pass (plan Task 12). Chat answers at AUTO.
_OMEGA_SETTLED_LANES = frozenset({"agent", "browser"})


def _omega_settles_turn(episode) -> bool:
    global_ = dict(getattr(episode, "global_", None) or {})
    return str(dict(global_.get("intent_decision") or {}).get("omega_lane") or "") in _OMEGA_SETTLED_LANES


def _close_settled_agent_job(episode, control) -> None:
    """Close an agent task's job once Omega settles it; a continuing task keeps its job open.

    The job is the agent lane's workflow, the browser workflow's equivalent: it spans the
    batches of one task, so AUTO no longer finishes it per batch. Failure-soft, and a job left
    open by a crash is stopped at the next boot by reconcile_jobs_at_boot.
    """
    auto = dict(getattr(episode, "auto", None) or {})
    job_id = str(dict(dict(auto.get("outputs") or {}).get("agent_run") or {}).get("job_id") or "")
    if not job_id:
        return
    from auto_loop.execute import job_control as jobs

    try:
        from pressure_loop.pressure import cancel_job_continuations, job_has_continuation

        if control is not None and control.decision == "PRESSURE" and job_has_continuation(job_id):
            return
        if jobs.get_job(job_id).status != "running":
            return
        if control is not None and control.decision == "ASK_OPERATOR":
            # The task is paused on its own question, not finished: the operator's answer
            # resumes it inside this job (operator 2026-09-15).
            return
        settled = control is not None and control.decision == "DONE"
        if settled:
            cancel_job_continuations(job_id, "Omega completed the job")
        jobs.settle_job_when_idle(
            job_id,
            status="completed" if settled else "stopped",
            summary=str(getattr(control, "synthesis", "") or "the work settled")[:500],
        )
    except Exception:  # noqa: BLE001 - a job receipt never fails the episode close
        log.warning("agent job close failed (failure-soft)", exc_info=True)


def _specialist_lane_origin(episode) -> dict | None:
    """The admitted Chat turn identity carried by a Browser/Coding lane episode."""
    pressure = dict(getattr(episode, "pressure", None) or {})
    for row in pressure.get("contributors") or ():
        if not isinstance(row, dict):
            continue
        detail = dict(row.get("detail") or row.get("metadata") or {})
        if not detail.get("lane_work_handoff"):
            continue
        lane = str(detail.get("lane") or "")
        cycle_id = str(detail.get("original_cycle_id") or "")
        handoff_id = str(detail.get("handoff_id") or "")
        thread_id = str(detail.get("thread_id") or "")
        if lane not in {"browser", "agent"} or not cycle_id or not handoff_id or not thread_id:
            raise ValueError("lane_work_dispatch has an incomplete origin identity")
        return {
            "lane": lane,
            "cycle_id": cycle_id,
            "handoff_id": handoff_id,
            "thread_id": thread_id,
            "browser_workflow_id": str(detail.get("browser_workflow_id") or ""),
        }
    return None


def _append_specialist_lane_update(episode, origin: dict, *, status: str, text: str,
                                   question: dict | None = None) -> None:
    from pressure_loop.operator_chat_responses import append_lane_update

    episode_id = str(getattr(episode, "episode_id", "") or "")
    try:
        append_lane_update(
            cycle_id=origin["cycle_id"],
            episode_id=episode_id,
            lane=origin["lane"],
            handoff_id=origin["handoff_id"],
            status=status,
            text=text,
            update_id=f"{episode_id}:{status}",
            question=question,
        )
    except Exception as exc:
        from memory.cognition_capture import capture_phase_failure

        capture_phase_failure(
            episode,
            phase="omega",
            event_kind="chat.lane_update_failed",
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise


def _settle_operator_turn(episode, control) -> None:
    """Stage Omega's successor, and answer a waiting agent or browser turn once the work settles.

    PRESSURE with a staged successor keeps the operator's bubble waiting; the successor
    carries it. DONE answers with the work's own reply or Omega's synthesis, ASK_OPERATOR
    puts Omega's question in that bubble instead of opening a separate successor, and a
    PRESSURE that could not be staged or a failed fold still answers, so the bubble never hangs.
    """
    from omega_loop.episode.transaction import publish_cognitive_transaction
    from pressure_loop.operator_chat_responses import waiting_chat_turn

    if control is None:
        from pressure_loop.continuation import incoming
        from pressure_loop.pressure import release_continuation

        release_continuation(incoming(episode), "the continuation's fold failed")

    meta = getattr(episode, "meta", None)
    lane_origin = _specialist_lane_origin(episode)
    turn = waiting_chat_turn(dict(getattr(episode, "pressure", None) or {})) if _omega_settles_turn(episode) else None
    cycle_id = str((turn or {}).get("cycle_id") or "")
    if not (turn or {}).get("chat_bubble_waiting") or (
            isinstance(meta, dict) and meta.get("chat_reply_delivered_cycle") == cycle_id):
        cycle_id = ""
    if control is not None:
        # T8: the fold publishes through ONE recoverable transaction record — opened before
        # any lane-entry mail is pinned, and the successor is staged from it. A question
        # that answers a waiting bubble stages no successor but still records its fold.
        stages = not ((cycle_id or lane_origin) and control.decision == "ASK_OPERATOR")
        successor = publish_cognitive_transaction(episode, control, stage_successor=stages)
        if stages and control.decision != "DONE" and successor is not None:
            # The successor runs the next batch inside the same job, so it stays open for it.
            if lane_origin is not None:
                lane_label = "Browser" if lane_origin["lane"] == "browser" else "Coding Agent"
                _append_specialist_lane_update(
                    episode,
                    lane_origin,
                    status="progress",
                    text=(str(getattr(control, "synthesis", "") or "").strip()
                          or f"The {lane_label} lane is continuing with the next step."),
                )
            return
    _close_settled_agent_job(episode, control)
    if lane_origin is not None:
        auto = dict(getattr(episode, "auto", None) or {})
        outputs = dict(auto.get("outputs") or {})
        lane_label = "Browser" if lane_origin["lane"] == "browser" else "Coding Agent"
        question = None
        if control is None:
            status = "failed"
            text = str(auto.get("failure_reason") or "The lane episode could not complete.")
        elif control.decision == "DONE":
            status = "completed"
            text = (str(outputs.get("response_text") or "").strip()
                    or str(getattr(control, "synthesis", "") or "").strip()
                    or f"The {lane_label} lane completed its work.")
        elif control.decision == "ASK_OPERATOR":
            status = "needs_input"
            run = dict(outputs.get("agent_run") or {})
            question_text = str(run.get("question") or getattr(control, "synthesis", "") or "").strip()
            text = (f"The {lane_label} lane needs your input: {question_text}"
                    if question_text else f"The {lane_label} lane needs your input.")
            if question_text:
                question = {
                    "question": question_text,
                    "options": list(run.get("options") or ())[:6],
                }
        else:
            status = "failed"
            text = str(getattr(control, "synthesis", "") or
                       f"The {lane_label} lane could not stage its next step.")
        if (lane_origin["lane"] == "browser" and control is not None
                and control.decision in {"DONE", "ASK_OPERATOR"}):
            from meta_loop.browser.workflow import mark_browser_waiting

            workflow = dict(dict(getattr(episode, "global_", None) or {}).get("browser_workflow") or {})
            workflow_id = str(workflow.get("workflow_id") or lane_origin.get("browser_workflow_id") or "")
            if workflow_id:
                mark_browser_waiting(workflow_id)
        _append_specialist_lane_update(
            episode, lane_origin, status=status, text=text, question=question
        )
        return
    if not cycle_id:
        return
    try:
        auto = dict(getattr(episode, "auto", None) or {})
        global_ = dict(getattr(episode, "global_", None) or {})
        if control is None:
            reason = str(auto.get("failure_reason") or dict(meta or {}).get("omega_fold_failure")
                         or "Omega could not reflect on the result")
            text = f"I could not complete that: {reason}"
        elif control.decision == "DONE":
            text = str(dict(auto.get("outputs") or {}).get("response_text") or "").strip() or control.synthesis
        else:
            text = control.synthesis
        from pressure_loop.operator_chat_responses import append_response

        payload = _operator_chat_async_payload(episode, text)
        if control is None:
            payload["delivery"]["failed"] = True
        question_payload = None
        if control is not None and control.decision == "ASK_OPERATOR":
            run = dict(dict(auto.get("outputs") or {}).get("agent_run") or {})
            if run.get("question") or run.get("options"):
                question_payload = {"question": str(run.get("question") or text),
                                    "options": list(run.get("options") or ()),
                                    "job_id": str(run.get("job_id") or "")}
        stored = append_response(
            cycle_id=cycle_id,
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            text=text,
            delivery=payload.get("delivery"),
            route=payload.get("route"),
            prompt_pack=payload.get("prompt_pack"),
            thinking=payload.get("thinking"),
            operator_question=question_payload,
            **_chat_correlation(episode),
        )
        if stored is None:
            log.warning("settled operator turn was not stored because the response rail is full")
            return
        if isinstance(meta, dict):
            meta["chat_reply_delivered_cycle"] = cycle_id
        lane = str(dict(global_.get("intent_decision") or {}).get("omega_lane") or "")
        if control is not None and control.decision in {"DONE", "ASK_OPERATOR"} and lane == "browser":
            from meta_loop.browser.workflow import mark_browser_waiting

            # The window stays open for the operator's next message; nothing runs until then.
            mark_browser_waiting(dict(global_.get("browser_workflow") or {}).get("workflow_id"))
        routed = dict(global_.get("operator_route") or {})
        if routed.get("route"):
            from global_loop.intent.context import remember_action

            remember_action(str(routed["route"]), str(routed.get("request") or ""),
                            thread_id=str(getattr(episode, "thread_id", "") or ""),
                            episode_id=str(getattr(episode, "episode_id", "") or ""))
    except Exception as exc:
        from memory.cognition_capture import capture_phase_failure

        capture_phase_failure(
            episode, phase="omega", event_kind="chat.delivery_failed",
            reason=f"{type(exc).__name__}: {exc}",
        )
        log.warning("settled operator turn delivery failed (failure-soft)", exc_info=True)


def _deliver_chat_reply_at_auto(episode) -> None:
    """Deliver the chat reply the moment AUTO finalizes it (operator 2026-07-03:
    "run the cascade after the action"). The operator's bubble resolves HERE;
    Meta B → Global B → Omega identity fold → DeepSleep run BEHIND the
    already-delivered reply. Stamps ``chat_reply_delivered_cycle`` on the episode
    so the close-time capture in `_record_operator_outcome` (kept as the safety
    net for episodes that never pass this seam) does not post a second bubble.
    RESOLVE_QUERY replies keep their close-time proactive-feed route (no chat
    cycle to resolve). Delivery failures become episode content while the
    learning phases continue behind the action."""
    try:
        auto = dict(getattr(episode, "auto", None) or {})
        outputs = dict(auto.get("outputs") or {})
        text = str(outputs.get("response_text") or "").strip()
        image = (
            dict(outputs["image_artifact"])
            if isinstance(outputs.get("image_artifact"), dict)
            else None
        )
        document = (
            dict(outputs["document_artifact"])
            if isinstance(outputs.get("document_artifact"), dict)
            else None
        )
        permission_raw = outputs.get("plugin_permission_request")
        permission = dict(permission_raw) if isinstance(permission_raw, dict) else None
        lane_work_receipt = outputs.get("specialist_handoff")
        if (permission is None and _omega_settles_turn(episode)
                and not isinstance(lane_work_receipt, dict)):
            # A specialist episode answers when Omega settles it (_settle_operator_turn).
            # A routed handoff already has its durable AUTO acknowledgement here; an
            # approval card still reaches the operator now, while the work waits on it.
            return
        failed_delivery = False
        if not (text or image or document or permission):
            # An AUTO that FAILED without a word still owes the operator its
            # bubble (live 2026-09-05: two "build" turns died in planning and the
            # HUD spun for seven minutes before printing a timeout note). The
            # failure IS the reply — honest, and the turn is resolved instead of
            # eaten. An autonomous episode with no operator turn returns below
            # exactly as before.
            failure_kind = str(auto.get("failure_kind") or "").strip()
            if not failure_kind:
                return
            detail = str(auto.get("failure_reason") or failure_kind).strip()
            text = f"I could not complete that: {detail}"
            failed_delivery = True
        global_ = dict(getattr(episode, "global_", None) or {})
        action_goal = dict(global_.get("intent_decision") or global_.get("action_goal") or {})
        is_resolve_query = str(action_goal.get("intent") or "") == "RESOLVE_QUERY"
        pressure = dict(getattr(episode, "pressure", None) or {})
        # ONE derivation of "which bubble is waiting", shared with the response
        # stage that streams tokens into it (RISK 5). Two copies of this rule
        # could stream an answer into one bubble and finish it in another, and
        # both halves would look right on their own. `None` covers both former
        # early returns: no operator contributor, and a fast-reply that already
        # answered inline (never a second bubble).
        from pressure_loop.operator_chat_responses import waiting_chat_turn

        turn = waiting_chat_turn(pressure)
        if turn is None:
            return
        cycle_id = str(turn.get("cycle_id") or "")
        chat_bubble_waiting = bool(turn.get("chat_bubble_waiting"))
        if is_resolve_query and not (cycle_id and chat_bubble_waiting):
            # Panel answer to a system query: no chat bubble waits, but the reply
            # still reaches the HUD NOW via the proactive pinboard (operator directive
            # 2026-07-11: HUD gets the response right after AUTO; the fold runs
            # BEHIND it — it must never gate delivery). Stamped so the close-time
            # safety net does not post a duplicate.
            meta = getattr(episode, "meta", None)
            if isinstance(meta, dict) and meta.get("resolve_reply_delivered"):
                return
            from pressure_loop.operator_chat_responses import remember_assistant_turn
            from pressure_loop.proactive_chat_messages import append_update

            episode_id = str(getattr(episode, "episode_id", "") or "")
            append_update(
                text=text,
                episode_id=episode_id,
                opened_by=str(getattr(episode, "opened_by", "") or ""),
                intent="RESOLVE_QUERY",
            )
            remember_assistant_turn(episode_id, text)
            if isinstance(meta, dict):
                meta["resolve_reply_delivered"] = True
            return
        if not cycle_id:
            return  # no originating chat cycle_id — nothing waiting to resolve
        from pressure_loop.operator_chat_responses import append_response

        payload = _operator_chat_async_payload(episode, text)
        if failed_delivery:
            payload["delivery"]["failed"] = True

        assignment = outputs.get("workspace_assignment")
        stored_response = append_response(
            cycle_id=cycle_id,
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            text=text,
            delivery=payload.get("delivery"),
            route=payload.get("route"),
            prompt_pack=payload.get("prompt_pack"),
            thinking=payload.get("thinking"),
            image=image,
            document=document,
            workspace_assignment=dict(assignment) if isinstance(assignment, dict) else None,
            plugin_permission_request=permission,
            **(
                {"lane_work": dict(lane_work_receipt)}
                if isinstance(lane_work_receipt, dict)
                else {}
            ),
            **_chat_correlation(episode),
        )
        if stored_response is None:
            log.warning("operator chat response was not stored because the response rail is full")
            return
        meta = getattr(episode, "meta", None)
        if isinstance(meta, dict):
            meta["chat_reply_delivered_cycle"] = cycle_id
        routed = dict(global_.get("operator_route") or {})
        if routed.get("route"):
            # The answered route becomes the previous assistant action Global A's
            # intent router reads beside the operator's next message.
            from global_loop.intent.context import remember_action

            remember_action(str(routed["route"]), str(routed.get("request") or ""),
                            thread_id=str(getattr(episode, "thread_id", "") or ""),
                            episode_id=str(getattr(episode, "episode_id", "") or ""))
    except Exception as exc:
        from memory.cognition_capture import capture_phase_failure

        capture_phase_failure(
            episode, phase="auto", event_kind="chat.delivery_failed",
            reason=f"{type(exc).__name__}: {exc}",
        )
        log.warning("deliver-at-AUTO chat reply failed (failure-soft)", exc_info=True)


def _mint_query_exchange_memory(
    episode,
    *,
    question: str,
    answer: str,
    reply: str,
    question_id: str = "",
    ask_episode_id: str = "",
) -> None:
    """Durable Q+A exchange record (operator 2026-07-10).

    The system's question, the operator's answer, and Omega's reply land in
    situational memory (lobe 4, 'what' facet) at episode close, so future
    recall sees what was talked about instead of re-living the amnesia.
    LINKED both ways (operator 2026-07-11): the record carries the open-question
    id and the ASK episode id in enrichment + provenance, so the graph compiler
    binds the exchange to the episode that asked — recall pulling either side
    pulls the whole conversation together. Intake is admitted to the current
    episode SRAM bucket; a failed intake aborts the episode with its cause intact.
    """
    question = str(question or "").strip()
    answer = str(answer or "").strip()
    reply = str(reply or "").strip()
    if not (answer or question):
        return
    try:
        from memory.api import CanonicalEnvelope, intake_memory
        from memory.dewey_map import SITUATIONAL_FACETS

        head = " ".join((question or answer).split())[:80]
        lines = []
        if question:
            lines.append(f"Omega asked: {question}")
        if answer:
            lines.append(f"Operator answered: {answer}")
        if reply:
            lines.append(f"Omega replied: {reply}")
        env = CanonicalEnvelope(
            cycle_id=str(getattr(episode, "episode_id", "") or "") or "resolve-query",
            coord=[4, SITUATIONAL_FACETS.index("what"), 0],
            type="operator_conversation",
            payload={
                "title": f"Operator Q&A — {head}",
                "summary": (
                    f"Omega asked '{head}'; operator answered: {' '.join(answer.split())[:120]}"
                ),
                "text": "\n".join(lines)[:4000],
                "subtopic": "operator_conversation",
                "tags": ["conversation", "operator", "system_query"],
                "enrichment": {
                    "question": question,
                    "answer": answer,
                    "reply": reply[:2000],
                    "episode_id": str(getattr(episode, "episode_id", "") or ""),
                    "question_id": str(question_id or ""),
                    "ask_episode_id": str(ask_episode_id or ""),
                    "event_kind": "operator_conversation",
                },
            },
            source="cognition_chain.resolve_query",
            confidence=0.9,
            provenance=[
                "cognition_chain._record_operator_outcome",
                *([f"system_query:{question_id}"] if question_id else []),
                *([f"ask_episode:{ask_episode_id}"] if ask_episode_id else []),
            ],
        )
        intake_memory(env, propose=False, bypass_guardian=True)
    except Exception:
        log.exception("Q&A exchange SRAM intake failed")
        raise


def _deliver_failed_chat_bubble(episode, meta: dict[str, Any]) -> bool:
    """Resolve a waiting operator bubble when a cascade failed before answering it.

    A failed cascade may stop before Global A produces an intent, so
    ``_record_operator_outcome`` cannot rely on the normal action-goal or response
    fields to find and finish the originating chat cycle. Keep this close-time
    safety net keyed by the same waiting-turn contract as the streaming rail.
    """
    cascade_failure = meta.get("cascade_failure")
    omega_failure = str(meta.get("omega_fold_failure") or "").strip()
    if not isinstance(cascade_failure, dict) and not omega_failure:
        return False

    pressure = dict(getattr(episode, "pressure", None) or {})
    from pressure_loop.operator_chat_responses import waiting_chat_turn

    turn = waiting_chat_turn(pressure)
    if not turn or not turn.get("chat_bubble_waiting"):
        return False
    cycle_id = str(turn.get("cycle_id") or "").strip()
    if not cycle_id or meta.get("chat_reply_delivered_cycle") == cycle_id:
        return False

    if isinstance(cascade_failure, dict):
        error_type = str(cascade_failure.get("error_type") or "CascadeFailure").strip()
        detail = str(cascade_failure.get("message") or "").strip()
    else:
        error_type = "OmegaFoldFailure"
        detail = omega_failure
    detail = " ".join(detail.split())[:500]
    reason = f" ({error_type})" if error_type else ""
    text = f"I could not complete this turn{reason}"
    if detail:
        text += f": {detail}"
    if not text.endswith("."):
        text += "."

    try:
        from pressure_loop.operator_chat_responses import append_response

        payload = _operator_chat_async_payload(episode, text)
        delivery = dict(payload.get("delivery") or {})
        delivery["failed"] = True
        stored = append_response(
            cycle_id=cycle_id,
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            text=text,
            delivery=delivery,
            route=payload.get("route"),
            prompt_pack=payload.get("prompt_pack"),
            thinking=payload.get("thinking"),
            **_chat_correlation(episode),
        )
    except Exception as exc:
        from memory.cognition_capture import capture_phase_failure

        capture_phase_failure(
            episode, phase="auto", event_kind="chat.delivery_failed",
            reason=f"{type(exc).__name__}: {exc}",
        )
        log.warning("failed cascade chat delivery could not be recorded", exc_info=True)
        return False
    if stored is None:
        log.error("failed cascade chat delivery was rejected by the response rail")
        return False

    meta["chat_reply_delivered_cycle"] = cycle_id
    episode.meta = meta
    return True


def _record_operator_outcome(store, episode) -> None:
    """Append cascade outcome to brain_turns for window reflection."""
    global_ = dict(getattr(episode, "global_", None) or {})
    meta = dict(getattr(episode, "meta", None) or {})
    auto = dict(getattr(episode, "auto", None) or {})
    _deliver_failed_chat_bubble(episode, meta)
    action_goal = dict(global_.get("intent_decision") or global_.get("action_goal") or {})
    if not action_goal:
        return
    pressure = dict(getattr(episode, "pressure", None) or {})
    contributors = list(pressure.get("contributors") or [])
    if not any(
        isinstance(row, dict)
        and str(row.get("id") or "") in ("operator_query", "operator_command", "operator_response")
        for row in contributors
    ):
        return
    intent = str(action_goal.get("intent") or "")
    execution_pack = dict(meta.get("execution_action_pack") or {})
    action = str(execution_pack.get("action") or "")
    outputs = dict(auto.get("outputs") or {})
    response_text = outputs.get("response_text")
    if response_text is None:
        artifacts = dict((global_.get("outcome") or {}).get("artifacts") or {})
        response_text = artifacts.get("response_text")
    cycle_id = str(getattr(episode, "episode_id", "") or "")
    found_operator_cycle_id = False
    reply_already_delivered = False
    chat_bubble_waiting = False
    thread_question = ""
    thread_question_id = ""
    thread_ask_episode_id = ""
    operator_message = ""
    for row in contributors:
        if not isinstance(row, dict):
            continue
        detail = dict(row.get("detail") or {})
        if not thread_question and detail.get("system_query_text"):
            thread_question = str(detail["system_query_text"])
            thread_question_id = str(detail.get("system_query_id") or "")
            thread_ask_episode_id = str(detail.get("system_query_episode_id") or "")
        if not operator_message and (detail.get("message") or detail.get("text")):
            operator_message = str(detail.get("message") or detail.get("text") or "")
        if detail.get("cycle_id"):
            cycle_id = str(detail["cycle_id"])
            found_operator_cycle_id = True
            reply_already_delivered = bool(detail.get("operator_reply_delivered"))
            chat_bubble_waiting = bool(detail.get("chat_bubble_waiting"))
            break
    if cycle_id and str(meta.get("chat_reply_delivered_cycle") or "") == cycle_id:
        reply_already_delivered = True  # answered at AUTO — the cascade folded behind it
    if _omega_settles_turn(episode):
        reply_already_delivered = True  # agent and browser turns are answered by _settle_operator_turn
    try:
        store.append_brain_turn(
            {
                "cycle_id": cycle_id,
                "ingress_class": "operator_outcome",
                "outcome_hint": str(auto.get("status") or "completed"),
                "action_kind": action or intent,
                "response_summary": {
                    "text": str(response_text or "")[:500],
                    "intent": intent,
                    "action": action,
                },
            }
        )
    except Exception:
        log.warning("operator outcome receipt append failed", exc_info=True)

    # Deliver the answer back to the HUD chat bubble that is waiting on it. An
    # operator-opened RESPOND episode carries response_text; route it to the
    # operator-chat-response store keyed by the ORIGINATING operator cycle_id
    # (never the answering episode_id) so the HUD resolves the right "thinking"
    # block. Terminal display only — never re-injected into pressure. Skip when
    # there is no real operator cycle_id (cannot correlate), no text (an
    # action-only command like LEARN, which is not a chat reply), OR the operator
    # was already answered inline by the fast-reply path (operator_reply_delivered):
    # the behind-cascade still saved memory + set trajectory but must NOT post a
    # second bubble. Failure-soft.
    if intent == "RESOLVE_QUERY" and str(response_text or "").strip():
        # Durable Q+A exchange (operator 2026-07-10): the thread — Omega's
        # question, the operator's answer, Omega's reply — lands in situational
        # memory as it happens, so future recall sees what was talked about.
        _mint_query_exchange_memory(
            episode,
            question=thread_question,
            answer=operator_message,
            reply=str(response_text or ""),
            question_id=thread_question_id,
            ask_episode_id=thread_ask_episode_id,
        )
        if chat_bubble_waiting and found_operator_cycle_id and not reply_already_delivered:
            # Chat-box answer (operator 2026-07-10): the operator answered the
            # system query in the MAIN chat, so a bubble IS waiting on the
            # operator cycle_id — resolve it like any chat reply. The proactive
            # feed below stays the rail for PANEL answers (no bubble waits).
            try:
                from pressure_loop.operator_chat_responses import append_response

                payload = _operator_chat_async_payload(episode, str(response_text or ""))
                append_response(
                    cycle_id=cycle_id,
                    episode_id=str(getattr(episode, "episode_id", "") or ""),
                    text=str(response_text or ""),
                    delivery=payload.get("delivery"),
                    route=payload.get("route"),
                    prompt_pack=payload.get("prompt_pack"),
                    thinking=payload.get("thinking"),
                    **_chat_correlation(episode),
                )
            except Exception as exc:
                from memory.cognition_capture import capture_phase_failure

                capture_phase_failure(
                    episode, phase="auto", event_kind="chat.delivery_failed",
                    reason=f"{type(exc).__name__}: {exc}",
                )
                log.warning("RESOLVE_QUERY chat-bubble delivery failed", exc_info=True)
            return
        # The operator ANSWERED a system query (via the system-messages panel) — there is no
        # chat-send cycle_id to resolve a "thinking" bubble against, and the HUD never polls
        # /api/chat/responses for a query answer. So Omega's REPLY to that answer would die
        # silently in operator_chat_responses (operator finding 2026-06-27). Route it to the
        # proactive pinboard feed, which the HUD polls unconditionally, so the reply reaches the window.
        if meta.get("resolve_reply_delivered"):
            return  # delivered at AUTO (operator 2026-07-11) — the fold ran behind it
        try:
            from pressure_loop.operator_chat_responses import remember_assistant_turn
            from pressure_loop.proactive_chat_messages import append_update

            episode_id = str(getattr(episode, "episode_id", "") or "")
            append_update(
                text=str(response_text or ""),
                episode_id=episode_id,
                opened_by=str(getattr(episode, "opened_by", "") or ""),
                intent=intent,
            )
            remember_assistant_turn(episode_id, str(response_text or ""))
        except Exception as exc:
            from memory.cognition_capture import capture_phase_failure

            capture_phase_failure(
                episode, phase="auto", event_kind="chat.delivery_failed",
                reason=f"{type(exc).__name__}: {exc}",
            )
            log.warning("RESOLVE_QUERY chat delivery failed", exc_info=True)
    elif found_operator_cycle_id and not reply_already_delivered and (
        str(response_text or "").strip()
        or (isinstance(outputs.get("plugin_permission_request"), dict))
    ):
        try:
            from pressure_loop.operator_chat_responses import append_response

            payload = _operator_chat_async_payload(episode, str(response_text or ""))
            assignment = outputs.get("workspace_assignment")
            permission_raw = outputs.get("plugin_permission_request")
            append_response(
                cycle_id=cycle_id,
                episode_id=str(getattr(episode, "episode_id", "") or ""),
                text=str(response_text or ""),
                delivery=payload.get("delivery"),
                route=payload.get("route"),
                prompt_pack=payload.get("prompt_pack"),
                thinking=payload.get("thinking"),
                workspace_assignment=dict(assignment) if isinstance(assignment, dict) else None,
                plugin_permission_request=(
                    dict(permission_raw) if isinstance(permission_raw, dict) else None
                ),
                **_chat_correlation(episode),
            )
        except Exception as exc:
            from memory.cognition_capture import capture_phase_failure

            capture_phase_failure(
                episode, phase="auto", event_kind="chat.delivery_failed",
                reason=f"{type(exc).__name__}: {exc}",
            )
            log.warning("operator chat response capture failed", exc_info=True)


def _settle_plugin_permission_decision(episode) -> None:
    """Release the queue owner after the decision episode reaches its terminus."""
    pressure = dict(getattr(episode, "pressure", None) or {})
    for row in pressure.get("contributors") or []:
        if not isinstance(row, dict) or str(row.get("id") or "") != "plugin_permission_decision":
            continue
        detail = dict(row.get("metadata") or row.get("detail") or {})
        request_id = str(detail.get("request_id") or "").strip()
        expected_revision = str(detail.get("expected_revision") or "").strip()
        if not request_id or not expected_revision:
            continue
        try:
            from pressure_loop.cognition_queue import settle_plugin_permission_decision

            if not settle_plugin_permission_decision(
                request_id,
                expected_revision=expected_revision,
            ):
                log.warning("plugin permission owner was not inflight: %s", request_id)
        except Exception:
            # Owner settlement is queue hygiene. Keep the episode close path
            # authoritative while making a capacity leak visible.
            log.warning("plugin permission owner settlement failed", exc_info=True)
        return


def _episode_id(obj) -> str:
    return str(getattr(obj, "episode_id", "") or "no-episode")


def _record_continuation_fact(episode) -> None:
    """The CONTINUED edge (spec section 9, T11), recorded when a staged successor ignites.

    Its source is the parent fold's episode and its target this continuation; its evidence is
    THIS episode's own canonical event — the continuation having happened is the proof. The
    obligation rides the successor's durable contributor row (``continued_from``, written at
    publication; ``successor_id`` and ``parent_episode_id`` were always there), so it survives
    a crash after staging and a B2 boot recovery, both of which the parent's carrier does not.
    The fact is an intake into the successor's own bucket: durable the instant it is accepted,
    and presented again under the same event id if this ignition is itself replayed. Never
    fails an ignition.
    """
    try:
        from memory.causal_edges import commit_causal_fact

        if str(getattr(episode, "opened_by", "") or "") != "omega_successor":
            return
        rows = dict(getattr(episode, "pressure", None) or {}).get("contributors") or ()
        detail = next((dict(row.get("detail") or {}) for row in rows
                       if isinstance(row, dict) and row.get("id") == "omega_successor"), None)
        if not detail or not detail.get("successor_id") or not detail.get("parent_episode_id"):
            return
        obligation = dict(detail.get("continued_from") or {})
        commit_causal_fact(
            episode, kind="continued", source=f"episode:{detail['parent_episode_id']}",
            target=f"continuation:{detail['successor_id']}",
            detail={"continuation_key": str(obligation.get("continuation_key") or ""),
                    "fold_id": str(obligation.get("fold_id") or ""),
                    "continuation_episode_id": _episode_id(episode)})
    except Exception:  # noqa: BLE001 - a fact never fails an ignition
        log.warning("continuation causal fact not recorded", exc_info=True)


def ignite_episode(store, ignition):
    """Handoff entry: pressure ignited → open the episode → start Global A.

    Each FCFS admission already owns one temporary physical pipeline. Its
    process-local lock guards only the substrate seams: episode open, exact
    queue acknowledgement, SRAM/epoch setup, and the DeepSleep/close terminus.
    The model-pass middle runs unlocked, so all four interchangeable pipelines
    may progress concurrently without semantic affinity.

    A pipeline-local terminus may reach the shared commit region before another
    pipeline that started earlier. The commit region remains serialized and its
    first statement stamps the true landing order; completion timing never
    changes the queue's admission order."""
    return _ignite_episode_cascade(store, ignition)


# Phase 4 cut 10 removed ``_archive_locked_reflection_window`` and its
# close-failure recovery twin. Both existed to file a locked window away
# UNREFLECTED from the ignition seam and the terminus, and both are now
# unreachable by construction: ``EpisodeStore`` double-buffers a window the
# instant it locks (pressure_loop/episode.py::_double_buffer_locked_window), so
# the store never holds a locked window for anyone to archive, and reflection
# work carries the staged one instead. Deleting them also kept archive work out
# of the admission seam at the top of every episode.


_OPERATOR_CONTRIBUTOR_IDS = ("operator_query", "operator_command", "operator_response")


def _operator_perception_from_episode(episode) -> str:
    """The operator's raw stated text for THIS episode, from the pressure snapshot's
    operator contributor (mirrors _record_operator_outcome's detail read). "" when the
    episode was not opened by an operator turn (autonomous episodes hold nothing)."""
    return str(_operator_perception_record_from_episode(episode).get("text") or "")


def _operator_perception_record_from_episode(episode) -> dict[str, Any]:
    """Structured chat intake carried by this episode's operator contributor."""
    pressure = dict(getattr(episode, "pressure", None) or {})
    for row in pressure.get("contributors") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") in _OPERATOR_CONTRIBUTOR_IDS:
            metadata = dict(row.get("metadata") or {})
            detail = {**metadata, **dict(row.get("detail") or {})}
            text = str(detail.get("message") or detail.get("text") or "").strip()
            if text:
                citation = detail.get("source_citation")
                return {
                    "text": text,
                    "ingress_class": str(detail.get("ingress_class") or ""),
                    "chat_intent": str(detail.get("chat_intent") or ""),
                    "source_citation": dict(citation) if isinstance(citation, dict) else {},
                    "pre_delivered_response_text": str(
                        detail.get("pre_delivered_response_text") or ""
                    ).strip(),
                }
    return {}


def _open_episode_sram(episode) -> None:
    """Open this episode's only intake bucket and move its queued payloads into it."""
    from memory import episode_sram
    from memory.api import CanonicalEnvelope

    episode_sram.open_bucket(_episode_id(episode), pipeline_id=episode.pipeline_id)
    for contributor in list((getattr(episode, "pressure", None) or {}).get("contributors") or []):
        metadata = dict((contributor or {}).get("metadata") or {})
        detail = {**metadata, **dict((contributor or {}).get("detail") or {})}
        for raw_envelope in list(detail.get("memory_envelopes") or []):
            if not isinstance(raw_envelope, dict):
                raise TypeError("queued memory envelope must be an object")
            episode_sram.hold_memory(
                CanonicalEnvelope(**raw_envelope),
                propose=False,
                bypass_guardian=True,
                skip_embed=False,
            )
        onboarding = detail.get("onboarding_profile")
        if onboarding is not None:
            if not isinstance(onboarding, dict):
                raise TypeError("queued onboarding profile must be an object")
            profile = onboarding.get("profile")
            if not isinstance(profile, dict):
                raise TypeError("queued onboarding profile fields must be an object")
            source_citation = onboarding.get("source_citation") or {}
            if not isinstance(source_citation, dict):
                raise TypeError("queued onboarding source citation must be an object")
            subcategories = onboarding.get("subcategories") or []
            if not isinstance(subcategories, list):
                raise TypeError("queued onboarding subcategories must be a list")

            from hud.operator_onboarding import seed_first_wake
            from memory.persona_ingest import ingest_operator_profile

            ingest_operator_profile(profile, source_citation=source_citation)
            seed_first_wake(
                str(profile.get("occupation") or ""),
                [str(item) for item in subcategories],
                source_citation=source_citation,
                operator_id="operator",
            )
    perception = _operator_perception_record_from_episode(episode)
    text = str(perception.get("text") or "")
    if text:
        episode_sram.hold_perception(
            text,
            speaker="operator",
            ingress_class=str(perception.get("ingress_class") or ""),
            chat_intent=str(perception.get("chat_intent") or ""),
            source_citation=dict(perception.get("source_citation") or {}),
        )
        episode_sram.hold_turn("user", text)
        pre_delivered = str(perception.get("pre_delivered_response_text") or "").strip()
        if pre_delivered:
            episode_sram.hold_turn("assistant", pre_delivered)


def _close_episode_sram(ep_id: str) -> None:
    """Write-free post-DeepSleep cleanup for an already-drained episode slot."""
    from memory import episode_sram

    # Cut 12: the epoch pin dies with the bucket it was captured beside. Dropped
    # FIRST and unconditionally — an episode whose bucket is already gone still
    # has a pin to release, and a pin outliving its episode would be handed to
    # the next unbound read on this thread (episode_sram's most-recent fallback)
    # as if it were that read's own snapshot.
    from memory.epoch import close_episode_epoch

    close_episode_epoch(ep_id)

    if not episode_sram.is_open(ep_id):
        return
    remaining = episode_sram.pending(ep_id)
    if remaining:
        raise RuntimeError(
            f"episode SRAM {ep_id} still has {len(remaining)} pending item(s) after DeepSleep"
        )
    episode_sram.clear_bucket(ep_id)


def _record_cascade_failure(episode, error_type: str, message: str) -> None:
    """Write the exception that failed the cascade onto the carrier (WO-06).

    The terminus journals a failed episode; without this it journaled ``cascade_status=failed``
    and nothing else, so the cause and the stage it struck were lost, and the stages after it
    disappeared from the record. The stage trail reads this to name the failed stage's cause.
    The first failure is kept: a later one in the same unwinding is its consequence.
    """
    meta = dict(getattr(episode, "meta", None) or {})
    if isinstance(meta.get("cascade_failure"), dict):
        return
    meta["cascade_failure"] = {"error_type": str(error_type or "Error"),
                               "message": str(message or "")[:1000]}
    episode.meta = meta


def _settle_turn_deadline_expiry(episode, error) -> None:
    """Turn an exhausted operator budget into a failed episode and terminal reply."""
    _record_cascade_failure(episode, "TurnDeadlineExpired", str(error))
    meta = dict(getattr(episode, "meta", None) or {})
    meta["turn_deadline_expired"] = {
        "cycle_id": str(getattr(error, "key", "") or ""),
        "stage": str(getattr(error, "stage", "") or ""),
        "budget_s": float(getattr(error, "budget_s", 0.0) or 0.0),
        "elapsed_s": float(getattr(error, "elapsed_s", 0.0) or 0.0),
        "overrun_s": float(getattr(error, "overrun_s", 0.0) or 0.0),
    }
    episode.meta = meta
    _deliver_failed_chat_bubble(episode, meta)


def _effective_cascade_status(episode, cascade_status: str) -> str:
    """Resolve late fold outcomes before journal, window, and close consumers.

    AUTO can intentionally stop before Meta B while the pressure loop is paused,
    and per-episode Omega contains contract/parse failures on the episode instead
    of raising. Both happen after the caller's initial completed value was passed
    down the cascade, so consumers must derive final state from the carrier.
    """
    meta = getattr(episode, "meta", None)
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("motor_planning_failure") or meta.get("omega_fold_failure"):
        return "failed"

    current = str(cascade_status or "completed")
    if current != "completed":
        return current
    if meta.get("meta_b_foldback_deferred"):
        return "deferred"
    return current


#: A self-directed cascade paces itself between phases so a chain of Omega's own episodes cannot
#: hammer the one cognition route continuously (operator 2026-09-22, measured: seven successors
#: back to back pushed an operator "hello" from 7.5 s to 28.8 s to its first word).
DEFAULT_AUTONOMOUS_PHASE_PAUSE_S = 3.0

#: A pause longer than this is indistinguishable from a hang, so it is capped rather than honoured.
MAX_AUTONOMOUS_PHASE_PAUSE_S = 30.0

#: Work somebody is waiting for. Never paced.
_OPERATOR_WORK_KINDS = frozenset({"operator_turn", "operator_query", "operator_command",
                                  "operator_response", "operator_lane_work"})
_OPERATOR_OPENERS = frozenset({"operator_query", "operator_command", "operator_response",
                               "operator_chat", "operator_priority"})


def autonomous_phase_pause_seconds() -> float:
    """How long a self-directed cascade waits between phases.

    Read per call so the operator can change it without a restart. A value that cannot be read as
    a number falls back to the default rather than to zero: a misconfigured pace should still
    pace. A negative value is no pause; an absurd one is capped.
    """
    raw = os.environ.get("OMEGA_AUTONOMOUS_PHASE_PAUSE_S")
    if raw is None or not str(raw).strip():
        return DEFAULT_AUTONOMOUS_PHASE_PAUSE_S
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("OMEGA_AUTONOMOUS_PHASE_PAUSE_S=%r is not a number; using the default", raw)
        return DEFAULT_AUTONOMOUS_PHASE_PAUSE_S
    if seconds <= 0:
        return 0.0
    return min(seconds, MAX_AUTONOMOUS_PHASE_PAUSE_S)


def _is_operator_work(episode) -> bool:
    work_kind = str(getattr(episode, "work_kind", "") or "")
    if work_kind in _OPERATOR_WORK_KINDS:
        return True
    return str(getattr(episode, "opened_by", "") or "") in _OPERATOR_OPENERS


def pace_autonomous_phase(episode, phase: str) -> None:
    """Wait a beat before *phase*, but only for work Omega chose for itself.

    Taken at the phase HANDOFF: the cascade is between phases here, holding nothing but its own
    lane — never a commit region, never a substrate seam. An operator turn is never paced, and an
    episode this cannot read is never paced either, because guessing would slow a turn somebody is
    waiting for.
    """
    try:
        if _is_operator_work(episode):
            return
        seconds = autonomous_phase_pause_seconds()
        if seconds <= 0:
            return
        log.debug("pacing self-directed %s by %.1fs before %s",
                  getattr(episode, "episode_id", "?"), seconds, phase)
        time.sleep(seconds)
    except Exception:
        log.debug("phase pacing skipped (non-fatal)", exc_info=True)


def _lanes_busy_at_admission(pipeline_id) -> int | None:
    """How many OTHER pipelines were already in flight when this turn was admitted.

    The demo bar's latency number is meaningless without it: "answered in 1.4 s" says nothing
    unless we know whether anything else was running. Returns ``None`` when it cannot be read —
    an unmeasured load must never present as an idle machine.
    """
    try:
        from pressure_loop.pipeline_dispatch import PIPELINE_IDS, pipeline_in_flight

        return sum(
            1 for pid in PIPELINE_IDS
            if pid != pipeline_id and pipeline_in_flight(pid)
        )
    except Exception:
        return None


def _admit_episode(store, ignition):
    """The admission seam: open the carrier, record its origin, acknowledge its request.

    Lifted verbatim out of ``_ignite_episode_cascade`` (WO-04) so the ACTUAL admission
    producer can be driven directly by a test instead of only through a whole cascade.
    Every statement inside the seam, and their order, is unchanged.

    **Runs under the caller's already-held physical pipeline lock.** That lock guards exactly
    this substrate phase, so acquiring or releasing it here would change the topology the
    caller documents.

    Returns ``(episode, ignition)``: the opened carrier, and the ignition rebound to its
    episode id, which is what the unlocked model-pass middle consumes.
    """
    _pipeline_id = ignition.pipeline_id
    pressure_snapshot = {
        "aggregate_pressure": ignition.aggregate_pressure,
        "opened_by": ignition.opened_by,
        "trajectory_snapshot": dict(ignition.trajectory_snapshot or {}),
        "neuromod_state": dict(ignition.neuromod_state or {}),
        "contributors": [
            {"id": c.type, "weight": c.weight, "detail": dict(c.metadata)}
            for c in ignition.contributors
        ],
    }
    # Capture before opening or acknowledging the request. An unavailable snapshot is
    # a failed admission, not an episode silently stripped of its context.
    from core.episode_temporal import capture_temporal_context
    from pressure_loop.symphony_learning_state import build_episode_learning_state

    admitted_metadata = dict(getattr(ignition.dominant_contributor, "metadata", None) or {})
    temporal_context = capture_temporal_context(
        phase="pressure", cycle_id=str(admitted_metadata.get("cycle_id") or "")
    )
    learning_state_context = build_episode_learning_state()

    with phase_span("pressure", "open_episode", episode_id="no-episode"):
        episode = store.open(
            opened_by=ignition.opened_by,
            pipeline_id=_pipeline_id,
            work_kind=ignition.work_kind,
            pressure=pressure_snapshot,
            temporal=temporal_context,
            learning_state=learning_state_context,
        )
    if episode is None:
        # Fail-hard: a failed episode open is a cognition failure — the episode
        # is the carrier for the whole cascade. We HALT rather than silently
        # skip (operator directive: cognition crashes the system).
        raise RuntimeError("episode open failed")
    if episode.pipeline_id != _pipeline_id:
        # The seam must never hold one physical pipeline's lock around an episode
        # bound to another pipeline. That would let two episodes interleave on
        # the same physical owner through seams meant to serialize them.
        # Fail-hard on turn one instead, the same posture as the ack mismatch
        # below.
        raise RuntimeError(
            f"lane lock mismatch: held {_pipeline_id!r} for ignition "
            f"{ignition.opened_by!r}, but the episode opened on "
            f"{episode.pipeline_id!r}"
        )
    # WO-04. The SRAM owner is opened HERE, before the acknowledgement, because acknowledgement
    # is irreversible — it removes the row from the one durable queue — and the origin event
    # cannot be recorded without its owner bucket. Opening it first makes the ordering
    # "durable origin, then consume the request", so a crash in that interval leaves a request
    # the queue still holds rather than one it says was taken and memory never recorded.
    # A failure here therefore leaves the exact request recoverable, by construction.
    _open_episode_sram(episode)
    from memory.cognition_capture import capture_admission_origin, capture_episode_opened

    capture_admission_origin(episode, ignition, ignition.dominant_contributor)
    # When this turn is the operator grading a result Omega presented for an EARLIER episode, that
    # verdict is recorded here, before the request is consumed: the episode it judges is closed and
    # its record sealed, so the observation belongs to this one and names that one. Any other turn
    # records nothing (memory/operator_feedback.py).
    from memory.operator_feedback import capture_operator_feedback

    capture_operator_feedback(episode, ignition.dominant_contributor)
    # The carrier now owns the admitted input. Acknowledge exactly its leased
    # queue_id before cognition; later FCFS items remain durable or may be leased
    # by another free physical pipeline (AGENTS.md §4.0).
    from pressure_loop.cognition_queue import (
        acknowledge_input,
        owns_input_lease,
    )

    dominant = ignition.dominant_contributor
    try:
        # Measurement only (demo bar B2): the turn now has a pipeline and an episode, so the
        # queue wait is closed. Marks never gate — a lost mark loses a number, not a turn.
        from pressure_loop.turn_latency import mark

        metadata = dict(getattr(dominant, "metadata", None) or {})
        mark(
            str(metadata.get("cycle_id") or ""),
            "admitted",
            pipeline_id=_pipeline_id,
            episode_id=getattr(episode, "episode_id", None),
            work_kind=getattr(ignition, "work_kind", "") or None,
            lanes_busy_at_admission=_lanes_busy_at_admission(_pipeline_id),
        )
    except Exception:
        log.debug("turn latency admitted mark not recorded (non-fatal)", exc_info=True)
    if owns_input_lease(dominant, _pipeline_id) and not acknowledge_input(dominant, _pipeline_id):
        raise RuntimeError("opened episode could not acknowledge its queue head")
    # A prepared Omega successor is consumed by successful episode admission,
    # never by pressure polling.
    try:
        from pressure_loop.pressure import release_staged

        # A pressure signal dies
        # HERE, at ignition, and nowhere else (operator 2026-08-15: "it needs to
        # die on episode ignition so omega can either emit another pressure
        # signal or go idle"). Staging used to die by being LOOKED AT — every
        # gather drained it — so a signal that lost one contest was destroyed
        # unheard, and the mind's volition evaporated without an episode.
        release_staged(ignition.dominant_contributor)
    except Exception:
        log.exception("omega successor acknowledgement failed after episode open")
        raise
    _attach_pending_system_events(episode)
    # A staged successor that ignites records that its parent CONTINUED into it (T11) — an
    # intake into the bucket just opened, so it is durable before anything else runs.
    _record_continuation_fact(episode)
    _record_operator_ingress(store, episode, ignition)
    # Phase 4 cut 12: the memory epoch is captured HERE, one statement after the
    # SRAM bucket, because the pin resolves through that bucket's binding — the
    # snapshot an episode reads at and the episode's own held writes have to
    # name one episode, and capturing them in the same instant inside the
    # admission seam is what makes them describe one moment instead of two.
    # Observational-first: nothing here changes what a recall returns, only what
    # the receipt can honestly say about it.
    from memory.epoch import open_episode_epoch

    _memory_epoch_pin = open_episode_epoch(episode)
    # WO-04. The starting state is recorded from the owners that just produced it: the carrier's
    # own opened_at, the pin this statement returned (None when epochs are off — declared
    # unavailable, never replaced by a later snapshot), and the ignition's trajectory reference.
    capture_episode_opened(
        episode,
        pin=_memory_epoch_pin,
        trajectory=getattr(ignition, "trajectory_snapshot", None),
    )
    # Durable job work carries its execution context with it. Lift the
    # envelope out of the ignition and onto the episode's own meta HERE, inside
    # the admission seam, so every later stage (motor planning's rebind, the
    # origin resolution, the completion receipt) reads one place — and so the
    # carried motor-attempt history is restored under the key the planner and
    # the journal already read, keeping the loop guard continuous across episodes.
    from pressure_loop.agent_lane import restore_dispatch

    restore_dispatch(episode)
    # The same lift applies to reflection work: the staged window it
    # was ignited for is carried onto the episode here, in the admission seam,
    # so the fold, the archive and the window-scope terminus all read one place.
    # NOT gated on OMEGA_WINDOW_LANE: an episode already dispatched must always
    # find its work, whatever the switch says now.
    from pressure_loop import window_lane

    window_lane.restore_window(episode)
    # Every model pass inherits the temporary physical pipeline selected by the
    # shared FIFO admission. Operator, autonomous, continuation, and reflection
    # work are interchangeable on P0-P3; ``work_kind`` remains descriptive.
    ignition = replace(ignition, episode_id=episode.episode_id)
    ep_id = _episode_id(episode)
    try:
        from guardian.telemetry import set_episode_id

        set_episode_id(ep_id)
    except Exception:
        pass
    return episode, ignition


def _ignite_episode_cascade(store, ignition):
    """Bind the accepted operator turn's one budget to every stage on this worker."""
    from pressure_loop import turn_deadline

    metadata = dict(getattr(ignition.dominant_contributor, "metadata", None) or {})
    cycle_id = str(metadata.get("cycle_id") or "").strip()
    deadline = turn_deadline.resolve(cycle_id)
    completed = False
    try:
        with turn_deadline.bind(deadline):
            result = _ignite_episode_cascade_bound(store, ignition)
        completed = result is not None
        return result
    finally:
        # A turn that never opened an episode remains queued/releasable under the same
        # accepted budget. Only the successful cascade return is an explicit terminal marker;
        # exceptions can leave a retryable FIFO continuation even after a store close.
        if completed:
            turn_deadline.finish(cycle_id)


def _ignite_episode_cascade_bound(store, ignition):
    # Cut 4 lock topology (see ignite_episode docstring): the lock guards the
    # ignition/admission substrate phase and the deepsleep/close terminus;
    # the model-pass middle runs unlocked. Explicit acquire/release keeps the
    # long-lived block structure (and its audit history) unchanged.
    #
    # Hold the already-leased physical pipeline's lock across ``store.open`` so
    # the new episode inherits that exact temporary owner. Admission chose the
    # pipeline FCFS; neither contributor type nor ``work_kind`` is consulted here.
    _pipeline_id = ignition.pipeline_id
    _substrate_lock = pipeline_cognition_lock(_pipeline_id)
    _substrate_lock.acquire()
    _substrate_held = True
    episode, ignition = _admit_episode(store, ignition)
    ep_id = _episode_id(episode)
    from pressure_loop import turn_deadline

    deadline = turn_deadline.current()
    if deadline is not None:
        turn_deadline.adopt(ep_id, deadline.key)
    # The seam's own import moved with it; this function's TERMINUS still reads the name.
    from pressure_loop import window_lane

    # Substrate admitted — release for the unlocked model-pass middle.
    _substrate_lock.release()
    _substrate_held = False
    # The physical pipeline is THINKING. Written here, the first statement outside the
    # ignition seam, because the envelope is a restoration hint and must never be
    # I/O inside a substrate seam. The ignition is passed so
    # the continuation is armed even on the kill-switch inline path, which never
    # passed through the dispatch that normally arms it.
    _pipeline_state.mark_inference(episode, ignition)
    try:
        cascade_status = "completed"
        try:
            # Restore the dev lobe's prior execution context before Global A reads
            # the continuation. This fallible read belongs inside the guarded
            # cascade: admission has already acknowledged this episode's queue item,
            # so its failure must still reach the same DeepSleep terminus.
            from pressure_loop.agent_lane import recall_dev_context

            recall_dev_context(episode)
            with phase_span("pressure", "cascade", episode_id=ep_id):
                # Every episode runs the canonical cascade, window reflection
                # included: its reflection is Omega's window-lane fold.
                if not _start_global_a(store, episode, ignition, cascade_status):
                    cascade_status = "failed"
                    _record_cascade_failure(
                        episode, "IntentMissing", "Global A produced no intent decision"
                    )
            cascade_status = _effective_cascade_status(episode, cascade_status)
        except Exception as cascade_exc:
            from pressure_loop.turn_deadline import TurnDeadlineExpired

            if isinstance(cascade_exc, TurnDeadlineExpired):
                cascade_status = "failed"
                _settle_turn_deadline_expiry(episode, cascade_exc)
            else:
                cascade_status = "failed"
                _record_cascade_failure(episode, type(cascade_exc).__name__, str(cascade_exc))
                raise
    finally:
        # Cut 11: COMMITTING is stamped BEFORE the gate, not after it. The
        # envelope then names the wait itself ("this pipeline is queued for the
        # terminus") instead of being written from inside a seam it is not
        # allowed to do I/O in — and a pipeline found here on the next boot is one
        # whose cognition finished, which is precisely why it is not re-armed.
        _pipeline_state.mark_committing(episode)
        # Terminus: re-acquire this physical pipeline's lock for the DeepSleep/
        # close substrate seam. Only this pipeline's own termini queue here, and
        # its local half of DeepSleep runs inside it so another pipeline's work
        # does not wait. The finish line is the commit region's first statement
        # (core.commit_region), which every pipeline reaches through one lock.
        _substrate_lock.acquire()
        _substrate_held = True
        try:
            from guardian.telemetry import set_episode_id

            set_episode_id(None)
        except Exception:
            pass
        pending_exc = sys.exc_info()
        try:
            if store.current() is not None:
                episode = store.current()
                ep_id = _episode_id(episode)
                _settle_plugin_permission_decision(episode)
                _record_operator_outcome(store, episode)
                _record_system_message(episode)
                _record_proactive_chat(episode)
                # Cut 9: the chat bubble that asked for this build was resolved
                # seconds after the ask (the dispatch acknowledgment), so an
                # agent episode's ending needs its own durable, badge-counted
                # record. Placed with the other operator-facing close records,
                # BEFORE the deepsleep terminus, so the operator learns the work
                # ended even if consolidation later fails.
                _announce_agent_completion(episode, cascade_status)
                # DeepSleep owns the episode terminus. The active carrier stays
                # leased until memory writes, reindex, and maintenance commit.
                # Cut 10: a window episode's terminus consolidates over the TEN
                # episodes it just reflected on, not over its own (empty) body —
                # but ONLY once the reflection actually committed. A fold that
                # failed leaves the window staged for a retry, and consolidating
                # it here would apply deltas the retry applies again.
                if window_lane.reflection_committed(episode):
                    _run_window_terminus(store, episode, cascade_status=cascade_status)
                else:
                    _run_per_episode_terminus(
                        store, episode, cascade_status=cascade_status
                    )
                # Idempotent lifecycle cleanup only. DeepSleep already flushed
                # and closed the bucket. If it failed, control never reaches
                # here and the intact batch remains available for diagnosis.
                _close_episode_sram(ep_id)
                # Risk 8 (ruling 2026-08-11): the per-episode mastery delta
                # used to be annotated onto the reflection window HERE,
                # after the terminus returned, holding only this pipeline's
                # cognition lock. The window it annotated was whichever one
                # ``EpisodeStore`` happened to be collecting at that instant
                # — and between the commit region's append and this line, a
                # concurrent pipeline's terminus could close the tenth episode,
                # lock the window and swap it into staging. The delta then
                # described an episode that window does not contain, or (for
                # a tenth episode, whose own window was already staged) was
                # dropped entirely.
                #
                # It now travels with the body inside the commit region
                # (deepsleep_loop/loop.py), which is the only place that
                # knows which window this episode joined. Cut 10's
                # window-episode exclusion is preserved by the same
                # statement: ``commit_reflection_window_append`` declines a
                # reflection episode outright, so a window fold's pack can
                # no more annotate the collecting window than it can join it.
                #
                # Cut 13's lock ordering is untouched — no new lock is taken
                # and the store lock is reached, as before, from inside the
                # commit lock. Own pipeline lock -> commit lock, never pipeline->pipeline.
        except Exception as close_exc:
            if pending_exc[0] is not None:
                log.exception("store close/reflection failed while cascade exception in flight")
            else:
                raise close_exc from close_exc
        finally:
            pass          # WO-04: the dead `dominant` assignment here was removed
    # Healthy terminus: release this physical pipeline. Exception paths
    # deliberately exit with the lock held — any cascade exception is
    # fail-hard BY LAW (the pressure loop's fatal relay crashes the brain on
    # its next tick, unblocked by this lock), so a briefly-held lock during
    # teardown never outlives the process it condemns.
    _substrate_lock.release()
    _substrate_held = False
    # Cut 11: closed. The continuation is dropped HERE and nowhere earlier —
    # this is the first instant at which the turn exists somewhere durable that
    # is not the envelope (its journal row landed inside the terminus above), so
    # dropping it any sooner would reopen the very window this cut closed.
    # Reached only on the healthy path, deliberately: a cascade that raised
    # leaves its envelope alone for the fatal relay to stamp FAILED.
    _pipeline_state.mark_idle(episode)
    return episode


def _run_per_episode_terminus(store, episode, *, cascade_status: str):
    """Hand the active episode to the mandatory DeepSleep terminus.

    Renamed 2026-09-22 from a name containing "journal write": it described a write this code has
    not performed for a long time, while its span WRAPS the whole terminus. Measured live it read
    12.7s median and 48s max — the largest number in the system — and cost real time chasing a
    twelve-second file append that does not exist. A phase name that misdescribes itself is the same
    defect class as a log that does.

    Fail-hard: journal and maintenance failures propagate and block the next
    pressure ignition while the episode remains available for retry.
    """
    ep_id = _episode_id(episode)
    episode.cascade_status = str(cascade_status or "completed")
    from deepsleep_loop.loop import run_deepsleep_for_episode

    with phase_span("pressure", "episode_terminus", episode_id=ep_id):
        pace_autonomous_phase(episode, "deepsleep")
        with phase_span("pressure", "deepsleep_episode_handoff", episode_id=ep_id):
            return run_deepsleep_for_episode(store, cascade_status=cascade_status)


def _commit_window_reflection(episode, omega_result) -> None:
    """Archive a window episode's reflection, then unstage: the window's commit point.

    The reflection is Omega's ordinary window-lane fold. It still cannot fail the
    window (operator ruling 2026-08-11): a fold contained as a failed thought left no
    reflection, so the DETERMINISTIC verdict counted from the ten episodes' own
    recorded outcomes is archived instead, stamped ``reflection_source``. Otherwise
    the still-staged window would re-ignite into the same failure after every restart.

    Archive, then unstage. The archive is keyed by window_id and replays; the
    window-scope DeepSleep in the terminus is not replayable, so the row is dropped
    between them. ``commit_reflection`` is total (RISK-9): on a total archive failure
    the window stays staged, ``reflection_committed`` reads False, and this episode
    closes through the ordinary per-episode terminus.
    """
    from omega_loop.window.run import (
        build_window_view,
        deterministic_window_verdict,
        emit_window_fallback,
    )
    from pressure_loop import window_lane

    window = window_lane.carried_window(episode)
    outcome = getattr(omega_result, "window_reflection", None)
    with phase_span("pressure", "window_commit", episode_id=_episode_id(episode)) as span:
        if not isinstance(outcome, dict):
            meta = dict(getattr(episode, "meta", None) or {})
            reason = str(meta.get("omega_fold_failure") or "omega window fold produced no reflection")
            view = build_window_view(window)
            emit_window_fallback(view, reason)
            outcome = deterministic_window_verdict(window, view=view, reason=reason)
        span.set(
            window_id=window.window_id,
            episode_count=int(window.episode_count),
            reflection_source=str(outcome.get("reflection_source") or ""),
            target_domain=str(dict(outcome.get("trajectory_update") or {}).get("target_domain") or ""),
        )
        window_lane.set_carried_outcome(episode, outcome)
        # The reflection is also this episode's own cognitive record: the journal row
        # and the enriched body read episode.omega.
        episode.omega = dict(outcome)
        window_lane.commit_reflection(episode, window, outcome)


def _run_window_terminus(store, episode, *, cascade_status: str):
    """Hand the reflected window to the WINDOW-scope DeepSleep terminus.

    Same fail-hard contract as the per-episode write. The window is already
    archived and unstaged by this point (see ``_commit_window_reflection``), so a
    failure here costs this window's consolidation and never re-reflects it.
    """
    ep_id = _episode_id(episode)
    episode.cascade_status = str(cascade_status or "completed")
    from deepsleep_loop.loop import run_deepsleep_for_window
    from pressure_loop import window_lane

    window = window_lane.carried_window(episode)
    outcome = window_lane.carried_outcome(episode)
    with phase_span("pressure", "episode_terminus", episode_id=ep_id):
        with phase_span("pressure", "deepsleep_window_handoff", episode_id=ep_id):
            return run_deepsleep_for_window(
                store,
                window,
                outcome,
                cascade_status=cascade_status,
            )


def _run_omega_episode_cognition(episode):
    """Per-episode Omega cognition — the fold-back mind.

    After Global B builds ``episode.global_['omega_packet']``, Omega makes the
    single per-episode model pass: comprehend the result, diff recall vs. result,
    ground mastery, set pressure. Its operator-facing text is patched into the
    outcome artifacts BEFORE the outcome is recorded/delivered, so every existing
    consumer (operator chat, journal) reads Omega's text unchanged.

    Failure posture (organism death #3, operator doctrine 2026-07-12): a fold
    that fails as a THOUGHT is contained here — the episode closes failed with
    NO omega result (never a fabricated stub) and the organism lives. Since
    2026-08-11 that is the whole ``EpisodeCognitionFoldError`` family, not just
    the contract/parse case: an unmapped action name and a model that returned
    nothing after its bounded retries join it. Both used to raise bare
    RuntimeErrors that killed the brain three times in one day.

    CONTAINMENT IS ALSO WHAT SAVES THE OPERATOR'S TURN, and this is the reason
    it is not merely a leniency. The queue lease is released at ADMISSION
    (``_ignite_episode_cascade`` acknowledges the dominant contributor before
    any stage runs), and the external turn is marked terminal only in the
    cascade's ``finally``, gated on DeepSleep having flushed. A fold that raises
    therefore takes the turn with it: the input is already gone from the queue,
    the terminus never runs, and ``complete_external_turn`` never marks it. Held
    here instead, the cascade reaches its terminus, ``_effective_cascade_status``
    reads the stamp below and closes the episode "failed", and the turn is
    marked terminal-failed — resolved and honest rather than eaten. (The
    operator's chat bubble itself resolved even earlier, at AUTO, so the reply
    was never the fold's to lose.)

    STILL FATAL, deliberately — these are substrate, not thought: a missing
    Global B packet and an upstream ``recall_error`` (both raise plain
    RuntimeErrors from the fold), SharedState/cascade corruption, and any
    failure in the memory-commit terminus. Nothing below widens to bare
    ``Exception``; only the fold's own bounded-failure type is held.
    """
    ep_id = _episode_id(episode)
    with phase_span("pressure", "omega_episode", episode_id=ep_id):
        from omega_loop.episode.run import (
            EpisodeCognitionFoldError,
            run_omega_for_episode,
        )
        from memory.cognition_capture import (
            OMEGA_FAILED,
            capture_phase_failure,
            new_invocation_id,
        )

        invocation = new_invocation_id()
        try:
            result = run_omega_for_episode(episode)
        except EpisodeCognitionFoldError as exc:
            reason = f"Omega fold raised {type(exc).__name__}: {exc}"
            capture_phase_failure(
                episode,
                phase="omega",
                event_kind=OMEGA_FAILED,
                reason=reason,
                invocation_id=invocation,
            )
            meta = getattr(episode, "meta", None)
            if not isinstance(meta, dict):
                meta = dict(meta or {})
                episode.meta = meta
            meta["omega_fold_failure"] = str(exc)[:300]
            log.warning(
                "omega episode fold failed (%s) — episode closes "
                "failed, no result applied: %s",
                type(exc).__name__,
                exc,
            )
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "omega_fold_failure_contained",
                    episode_id=ep_id,
                    summary=str(exc)[:200],
                    status="degraded",
                    ok=False,
                    # Which KIND of thought failed. Same event name (dashboards
                    # read it) — a contained no-output must not be mistaken for
                    # a contract violation when the operator reads back a day
                    # of folds.
                    failure_kind=type(exc).__name__,
                )
            except Exception:
                pass
            return None
        from omega_loop.episode.packet import omega_mail_ownership_lost

        if omega_mail_ownership_lost(episode):
            # T8: the lease on the mail this fold reasoned over lapsed during inference, so
            # another fold may own those words now. Refusing the late acknowledgement would
            # protect only the mail's deletion; nothing DERIVED from it may be published
            # either — no memory commit, no lane-entry mail, no continuation. Contained like
            # any other fold that produced nothing usable.
            meta = getattr(episode, "meta", None)
            if not isinstance(meta, dict):
                meta = dict(meta or {})
                episode.meta = meta
            meta["omega_fold_failure"] = ("the operator mail this fold reasoned over was no "
                                          "longer its own when it finished")
            log.warning("omega episode fold discarded: bound mail ownership lapsed (%s)", ep_id)
            try:
                from guardian.terminal_feedback import emit_data

                emit_data("pressure", "omega_fold_failure_contained", episode_id=ep_id,
                          summary=meta["omega_fold_failure"], status="degraded", ok=False,
                          failure_kind="MailOwnershipLapsed")
            except Exception:
                pass
            return None
        from global_loop.intersections import MailOwnershipLost
        from omega_loop.episode.packet import FoldReplayRefused

        try:
            effective = _apply_omega_episode_result(episode, result)
            return effective if effective is not None else result
        except FoldReplayRefused as exc:
            # A refused replay with no frozen result on record: there is no authoritative
            # conclusion to act on, and its own may not be applied. Contained like any fold
            # that produced nothing usable.
            meta = getattr(episode, "meta", None)
            if not isinstance(meta, dict):
                meta = dict(meta or {})
                episode.meta = meta
            meta["omega_fold_failure"] = f"this fold is a refused replay ({exc}); nothing was applied"
            log.warning("omega episode fold discarded: refused replay (%s) %s", exc, ep_id)
            try:
                from guardian.terminal_feedback import emit_data

                emit_data("pressure", "omega_fold_failure_contained", episode_id=ep_id,
                          summary=meta["omega_fold_failure"], status="degraded", ok=False,
                          failure_kind="FoldReplayRefused")
            except Exception:
                pass
            return None
        except MailOwnershipLost:
            # The lease ran out between that check and memory acceptance, which is fenced
            # by ownership itself (_ingest_fold_knowledge): nothing was committed and the
            # carrier is untouched. Contained exactly like the lapse found above.
            meta = getattr(episode, "meta", None)
            if not isinstance(meta, dict):
                meta = dict(meta or {})
                episode.meta = meta
            meta["omega_fold_failure"] = ("the operator mail this fold reasoned over was no "
                                          "longer its own when its conclusions were committed")
            log.warning("omega episode fold discarded at memory acceptance: bound mail "
                        "ownership lapsed (%s)", ep_id)
            try:
                from guardian.terminal_feedback import emit_data

                emit_data("pressure", "omega_fold_failure_contained", episode_id=ep_id,
                          summary=meta["omega_fold_failure"], status="degraded", ok=False,
                          failure_kind="MailOwnershipLapsed")
            except Exception:
                pass
            return None
        return result


def _effective_fold_result(episode, result):
    """The ONE authoritative result of this fold — resolved before ANYTHING reads its output.

    A fold's first result is frozen whole in its durable manifest (cross-lane T8), under the
    same ownership fence as memory acceptance. A replay's freshly inferred result is then
    never an authority for anything: the outcome artifacts, the build lifecycle, the memory
    intake, the publication and the settlement all act on the ORIGINAL. Freezing the control
    on the carrier alone left the other consumers reading the replay's result — a frozen
    PRESSURE beside a build ported to completed (review 2026-09-17).

    A refused replay records nothing: it acts on the frozen result if one exists, and is
    discarded (``FoldReplayRefused``) if none does.
    """
    from global_loop import intersections as mail_store
    from omega_loop.episode.packet import (FoldReplayRefused, fold_identity, fold_replay_refused,
                                           while_omega_mail_owned)
    from omega_loop.episode.types import OmegaEpisodeResult

    if not isinstance(result, OmegaEpisodeResult):
        return result                      # not a fold result this machinery can freeze
    fold_id = fold_identity(episode)
    refused = fold_replay_refused(episode)
    if refused:
        frozen = dict(mail_store.fold_binding(fold_id) or {}).get("manifest") if fold_id else None
        if frozen is None:
            raise FoldReplayRefused(refused)
    else:
        # The host-known DERIVATION is frozen with the result, before intake (spec section 7):
        # a replay restores the original conclusion, and must restore what it rested on too.
        frozen = while_omega_mail_owned(episode, lambda: mail_store.record_fold_manifest(
            fold_id, {"result": result.to_dict(), "derivation": _fold_derivation(episode)}))
    if frozen is None:
        return result                      # no binding: nothing can replay this fold
    return OmegaEpisodeResult.from_dict({**frozen["result"], "episode_id": result.episode_id})


def _apply_omega_episode_result(episode, result):
    """Store the Omega result on the episode + patch outcome artifacts so the
    operator-facing response is delivered from the mind layer (Omega), not Auto.

    The artifact keys are the ones the close-time delivery
    (``_record_operator_outcome``) and the journal read: ``response_text`` then
    ``tool_result_summary`` as fallback.

    Returns the EFFECTIVE result — the one every consumer below, and the caller, acts on.
    """
    result = _effective_fold_result(episode, result)
    global_ = dict(getattr(episode, "global_", None) or {})
    global_["omega_result"] = result.to_dict()
    if result.cognized:
        outcome_pack = dict(global_.get("outcome") or {})
        artifacts = dict(outcome_pack.get("artifacts") or {})
        # An ACK folds back for PRESSURE only: the operator already saw AUTO's
        # acknowledgment ("On it — …"), so Omega must NOT overwrite it with a second
        # reply. Its pressure_params + intent_decision (applied below) still drive the
        # next action. Every other fold-back (skill / learn / apply / proactive / tool)
        # still delivers Omega's mind-layer text as before.
        auto_pack = dict(dict(getattr(episode, "auto", None) or {}).get("prompt_pack") or {})
        ack_foldback = str(auto_pack.get("prompt_type") or "") == "RESPONSE_ACK"
        if result.response_text is not None and not ack_foldback:
            artifacts["response_text"] = result.response_text
        if result.synthesis is not None:
            artifacts["tool_result_summary"] = result.synthesis
        outcome_pack["artifacts"] = artifacts
        global_["outcome"] = outcome_pack
    # MEMORY ACCEPTANCE COMES FIRST, and before the carrier changes (cross-lane T8): it is
    # fenced by the fold's ownership of the mail it reasoned over, and a fold that lost that
    # ownership must leave NOTHING behind — not a receipt, not a result on the carrier, not
    # a lifecycle advance (review 2026-09-17). ``global_`` is still a private copy here.
    _ingest_fold_knowledge(episode, result, global_)
    episode.global_ = global_
    _advance_proactive_lifecycle(episode, result)
    # MEMORY IS FIRST AND TRUTH (operator 2026-07-11): comprehended fold statements
    # are written to SITUATIONAL (4,1 "what") IN-EPISODE with committed receipts
    # riding omega_result into the journal/audit — knowledge/coding lobes stay
    # reserved for FETCHED content. Zero commits against claimed knowledge FAILS
    # HARD (unrecorded action = system failure); with nothing claimed, any
    # knowledge-mastery delta is stripped — prose earns nothing. (Performed above, fenced.)
    _apply_onboarding_subtopics(episode, result)
    _persist_motor_attempt_and_pressure(episode, result)
    # DETERMINISTIC build lifecycle (operator 2026-07-02): finalize, stop, or leave open the
    # build this episode's SRAM report represents, per Omega's control decision.
    _apply_build_report_decision(episode, result)
    # Omega's validated control decision is the only pressure authority (plan Task 13): it
    # stages at most one successor in _settle_operator_turn. The projected pressure_params and
    # intent_decision ride omega_result for the journal only and never ignite work.
    # 2026-06-30: shadow-record the episode's trajectory_influence (richer schema) into the
    # window-influence store. The 10-window self-authoring aggregates these — NOT pressure.
    _record_trajectory_influence(episode, result)
    # Step 8: record the episode outcome to the SignalEvent audit ledger (record-only,
    # failure-soft, gated SIGNALS_RECORD=False -> no-op). Substrate, not cognition.
    _record_signal_omega(episode, result)
    return result


def _apply_onboarding_subtopics(episode, result) -> None:
    """Turn a validated first-fold plan into the next episode's mastery gaps."""
    rows = tuple(getattr(result, "onboarding_subtopics", ()) or ())
    if not rows:
        return
    from hud.operator_onboarding import seed_foldback_subtopics
    from omega_loop.episode.packet import onboarding_context_from_pressure

    context = onboarding_context_from_pressure(getattr(episode, "pressure", None))
    if context is None:
        raise RuntimeError("onboarding foldback lost its originating context")
    seeded = seed_foldback_subtopics(context, rows)
    if not seeded:
        raise RuntimeError("onboarding foldback produced no durable child mastery gaps")

    global_ = dict(getattr(episode, "global_", None) or {})
    omega_result = dict(global_.get("omega_result") or {})
    omega_result["onboarding_seeded"] = seeded
    global_["omega_result"] = omega_result
    episode.global_ = global_


def _advance_proactive_lifecycle(episode, result) -> None:
    """Advance a bound operator policy only from validated Omega fold evidence."""
    global_ = dict(getattr(episode, "global_", None) or {})
    packet = dict(global_.get("omega_packet") or {})
    policy = packet.get("proactive_policy")
    if not isinstance(policy, dict):
        return
    thread_id = str(policy.get("thread_id") or "").strip()
    policy_id = str(policy.get("policy_id") or "").strip()
    if not thread_id or not policy_id:
        return

    from pressure_loop import proactive_lifecycle as lifecycle

    proactive_result = getattr(result, "proactive_result", None)
    if isinstance(proactive_result, dict):
        try:
            lifecycle.record_result(proactive_result)
        except (TypeError, ValueError):
            # Prompt parsing normally validates this contract. Contain any
            # malformed injected/legacy packet at the operational seam so it
            # cannot terminate episode close.
            log.warning("invalid proactive result rejected", exc_info=True)
        return

    plan = getattr(result, "operator_policy_plan", None)
    if isinstance(plan, dict):
        try:
            lifecycle.structure_operator_policy(policy_id=policy_id, **plan)
            lifecycle.mark_learning(
                thread_id,
                episode_id=str(getattr(result, "episode_id", "") or ""),
            )
        except (TypeError, ValueError):
            # A grade answer can arrive while the thread sits in a state the
            # strict machine rejects; the ledger stays strict, the cascade
            # stays alive (same containment class as record_result above).
            log.warning("proactive policy progression rejected", exc_info=True)
        return

    if str(packet.get("intent") or "") != "LEARN":
        return
    if lifecycle.thread_state(thread_id) not in {"learning", "continuing"}:
        return
    assessment = getattr(result, "episode_assessment", None)
    changed = getattr(result, "what_changed", None)
    if not isinstance(assessment, dict) or assessment.get("completed") is not True:
        return
    if not isinstance(changed, dict) or not any(
        changed.get(key) for key in ("learned", "did", "verified")
    ):
        return
    try:
        lifecycle.mark_applying(
            thread_id,
            episode_id=str(getattr(result, "episode_id", "") or ""),
        )
    except (TypeError, ValueError):
        log.warning("proactive applying transition rejected", exc_info=True)


_DELTA_KINDS = frozenset({"decision", "user_constraint"})


# A conclusion's MANDATORY references: the episode it arose in, and the operator turn a
# constraint restates.
_MANDATORY_DERIVATION_REFS = 2


def _recall_ref_capacity() -> int:
    """How many recalled memories one conclusion's derivation can name INLINE: what the write
    boundary holds, less the mandatory references and every mail row the binder can admit.
    Derived from the real limits on both sides, never a number of its own."""
    from memory.cognition_write import MAX_DERIVED_FROM
    from omega_loop.episode.types import CROSS_LANE_MAIL_ROWS

    return MAX_DERIVED_FROM - CROSS_LANE_MAIL_ROWS - _MANDATORY_DERIVATION_REFS


def _recalled_memory_ids(packet) -> list:
    """Every memory recall showed this fold. COMPLETE — never cut.

    The packet's ``recall`` rows are the recall tools' results; each carries its RESPONSIVE
    ``evidence`` with the ``memory_id`` the verifier resolved
    (``auto_loop.mril_tools.remember``). Non-responsive evidence is not support, so it is not
    named. Order preserved, duplicates dropped.
    """
    ids: list = []
    for row in (packet.get("recall") or ()) if isinstance(packet, dict) else ():
        for item in (row.get("evidence") or ()) if isinstance(row, dict) else ():
            memory_id = str(item.get("memory_id") or "").strip() if isinstance(item, dict) else ""
            if memory_id and memory_id not in ids:
                ids.append(memory_id)
    return ids


def _fold_derivation(episode) -> dict:
    """What this fold's conclusions rest on, as the HOST knows it: the episode they arose in,
    every memory recall showed it, and the mail it bound and still holds. Frozen in the fold's
    manifest beside its result."""
    global_ = dict(getattr(episode, "global_", None) or {})
    bound = dict(global_.get("omega_mail") or {})
    # One entry past the capacity is kept so that "too much to name" stays DETECTABLE after a
    # restart; a fold in that state accepts no statement at all (``_derivation_standing``), so
    # no evidence a record cites is ever among what is not kept. Without the bound a very
    # large recall would breach the manifest's size limit and fail the fold outright.
    return {"episode_id": _episode_id(episode),
            "recall": _recalled_memory_ids(global_.get("omega_packet"))[:_recall_ref_capacity() + 1],
            "mail": ([str(one) for one in bound.get("bound_mail_ids") or ()]
                     if bound.get("disposition") == "bound" else [])}


def _effective_derivation(episode) -> tuple:
    """``(derivation, unsupported)`` — the fold's FROZEN derivation when it has one, and why
    its statements cannot be honestly derived when they cannot.

    A replay recovering a statement its fold originally declared must journal it as derived
    from the ORIGINAL evidence, not from whatever the replay's own packet recalled: the first
    attempt declared [A, B] over one memory, journalled A and crashed; the replay recovered
    B's original wording and journalled it as derived from a memory only the replay had seen
    (review 2026-09-17).

    THREE cases, kept apart:
    - a frozen result WITH its derivation — this run or a replay: the frozen evidence;
    - NO frozen result at all (no durable identity): a genuinely new fold nothing can replay,
      which rests on what its own packet shows;
    - a frozen result WITHOUT a derivation: every fold freezes both since T10, so this can
      only be a REPLAY of a fold frozen before it. Its original evidence is unknown, and the
      replay's packet is not it — recovery of its unfinished statements is UNSUPPORTED rather
      than given the replay's provenance (review 2026-09-17).
    """
    from global_loop import intersections as mail_store
    from omega_loop.episode.packet import fold_identity

    fold_id = fold_identity(episode)
    manifest = dict(mail_store.fold_binding(fold_id) or {}).get("manifest") if fold_id else None
    if not isinstance(manifest, dict):
        return _derivation_standing(_fold_derivation(episode))
    frozen = manifest.get("derivation")
    if isinstance(frozen, dict) and frozen.get("episode_id"):
        return _derivation_standing(frozen)
    return _fold_derivation(episode), (
        "this fold's result was frozen before derivations were; its original evidence is "
        "unknown, and a replay's own evidence is not a substitute")


def _derivation_standing(derivation) -> tuple:
    if len(derivation.get("recall") or ()) > _recall_ref_capacity():
        return derivation, (f"the conclusion rests on more than {_recall_ref_capacity()} recalled "
                            "memories; a record names its evidence completely or not at all")
    return derivation, ""


def _derivation_refs(derivation) -> list:
    """The references one conclusion names besides its mandatory ones: every recalled memory
    and every bound mail row, COMPLETE and inline. There is no placeholder: a fold whose
    evidence does not fit never gets here (``_derivation_standing``)."""
    return ([f"memory:{one}" for one in derivation.get("recall") or ()]
            + [f"mail:{one}" for one in derivation.get("mail") or ()])


# What an acknowledged message's edge is called, by the mail's purpose (spec section 9).
_MAIL_EDGE_KINDS = {
    "operator_instruction": "modified",
    "specialist_lane_handoff": "modified",
    "lane_evidence": "informed",
}


def _record_fold_causal_facts(episode, global_, receipts) -> None:
    """The fold's causal edges, from HOST-KNOWN links only (spec section 9, T11).

    ``modified`` / ``informed``: an accepted statement rests on the mail this fold bound —
    its DURABLE derivation (the intake journal record's provenance, T10) names the mail — so
    the operator's words MODIFIED it, or lane evidence INFORMED it; the target is the accepted
    statement's journal record, never the episode. ``superseded``: an accepted decision named
    a record it supersedes AND its durable derivation names that record (the host had put it
    in front of the fold); a proposal the host cannot resolve is no edge.

    Evidence is read from the RECORD, never from the fold's in-memory derivation: a replay of
    a fold frozen before derivations were (unsupported recovery, T10) re-reports statements
    accepted by an earlier attempt, and its own packet is not their evidence. A fold that
    accepted nothing edges nothing, whatever it acknowledges. Recorded at acceptance rather
    than at the acknowledgement seam, which runs after the episode is archived, when no SRAM
    bucket is open to take an intake; the statement's acceptance is when the link becomes a
    fact.

    EACH FACT IS ITS OWN INTAKE RECORD (memory.causal_edges), so its obligation and its
    completion are the record's own, never inferred from the statement's status — boot
    released a statement, another episode adopted and committed it, and no causal-fact record
    existed to recover; a real drain committed a statement beside a still-pending fact
    (review 2026-09-17). A re-reported statement's facts are presented again under the same
    fold-stable event ids: the journal hands back the record on file, pending or committed,
    and nothing is duplicated; a fact whose first attempt died before it was journalled is
    journalled now. ONE batch: a refusal leaves nothing partial. The purposes come from the
    mail this fold binds, which for a replay is the identical set (T8); a message this fold
    does not hold gives no edge.
    """
    from memory import intake_journal
    from memory.causal_edges import commit_causal_facts
    from memory.cognition_write import DERIVED_FROM_PREFIX

    bound = dict(global_.get("omega_mail") or {})
    purposes = {str(row.get("mail_id")): str(row.get("purpose") or "")
                for row in bound.get("mail") or () if isinstance(row, Mapping)}
    facts: list = []
    for row in receipts:
        if not row.get("accepted") or not row.get("journal_record_id"):
            continue
        record = intake_journal.record_by_id(str(row["journal_record_id"]))
        if record is None:
            continue
        rests_on = [str(ref)[len(DERIVED_FROM_PREFIX):]
                    for ref in dict(record.payload or {}).get("provenance") or ()
                    if str(ref).startswith(DERIVED_FROM_PREFIX)]
        target = f"intake:{row['journal_record_id']}"
        for ref in rests_on:
            kind_of, _, identity = ref.partition(":")
            if kind_of == "mail":
                edge = _MAIL_EDGE_KINDS.get(purposes.get(identity, ""))
                if edge:
                    facts.append({"kind": edge, "source": ref, "target": target,
                                  "detail": {"purpose": purposes[identity],
                                             "statement_kind": row.get("kind")}})
        superseded = str(row.get("supersedes_id") or "")
        if superseded and f"memory:{superseded}" in rests_on:
            facts.append({"kind": "superseded", "source": target, "target": f"memory:{superseded}",
                          "detail": {"statement_kind": "decision"}})
    if facts:
        commit_causal_facts(episode, facts)


def _ingest_fold_knowledge(episode, result, global_) -> None:
    """Deterministic fold-time knowledge writes + receipt-gated knowledge mastery.

    Every statement the per-episode Omega fold produced is written NOW to the
    situational lobe at (4,1) "what" (memory.knowledge_ingest — the mind's own
    comprehension; knowledge/coding lobes are reserved for fetched content), and
    the write receipts are stamped into omega_result so the journal and audit
    trail carry proof the learning exists. Fail-HARD inside
    ingest on zero commits (memory is first and truth — substrate poison class,
    never folded). When the fold claimed NO knowledge, any positive
    knowledge_mastery delta is stripped before the journal/mastery consumers
    read it — mastery only ever moves on receipts, never prose."""
    omega_dict = global_.get("omega_result")
    if not isinstance(omega_dict, dict):
        return
    ep_id = _episode_id(episode)
    products = list(getattr(result, "knowledge_to_ingest", None) or ())
    # COGNITIVE DELTA (cross-lane spec sections 5-6, T8): decisions and user constraints
    # commit through the SAME receipted path as any fold statement — same lobe, same verdict
    # gate — and their receipts gate what the fold may publish afterwards
    # (omega_loop.episode.transaction). Intake events are keyed by the fold's stable
    # identity, so a replay of the fold re-presents them instead of recording them twice.
    packet = global_.get("omega_packet")
    delta_domain = str(packet.get("domain") or "") if isinstance(packet, dict) else ""
    delta = [{**row, "kind": "decision", "domain": delta_domain, "confidence": 0.8}
             for row in getattr(result, "decisions", None) or ()]
    delta += [{**row, "kind": "user_constraint", "domain": delta_domain, "confidence": 0.9}
              for row in getattr(result, "user_constraints", None) or ()]
    from omega_loop.episode.packet import fold_replay_refused

    refused = fold_replay_refused(episode)
    if refused and (products or delta):
        # The stores cannot vouch that this fold never ran (T8 replay refusal): it accepts
        # NOTHING into memory — the ONE gate is ``if not refused`` below. With nothing
        # recorded, mastery is gated below as unclaimed.
        try:
            from guardian.terminal_feedback import emit_data

            emit_data("pressure", "knowledge_ingest", episode_id=ep_id, status="degraded", ok=False,
                      summary=f"fold replay refused ({refused}); no statements accepted",
                      statements=len(products) + len(delta))
        except Exception:
            pass
    statements = [p if isinstance(p, dict) else
                  {"kind": "fold_statement", "domain": str(getattr(p, "domain", "") or ""),
                   "statement": str(getattr(p, "statement", "") or ""),
                   "confidence": float(getattr(p, "confidence", 0.5) or 0.5)}
                  for p in products] + delta
    receipts = []
    derivation: dict = {}
    if not refused:
        from memory.knowledge_ingest import ingest_knowledge_products
        from omega_loop.episode.packet import while_omega_mail_owned
        from omega_loop.episode.transaction import transaction_fold_id

        def accept():
            # ``result`` is already the fold's EFFECTIVE result (_effective_fold_result): for
            # a replay these are the statements the fold ORIGINALLY made, slot for slot — an
            # unfinished slot is recovered from the original payload, a filled one keeps its
            # first statement, and a fold that committed nothing gains nothing.
            if not statements:
                return []
            # What the fold's conclusions rest on (spec section 7): the memories recall put in
            # front of it and the mail it bound — host-known evidence, never model output, and
            # for a replay the ORIGINAL fold's, frozen beside its result.
            nonlocal derivation
            derivation, unsupported = _effective_derivation(episode)
            if unsupported:
                try:
                    from guardian.terminal_feedback import emit_data

                    emit_data("pressure", "knowledge_ingest", episode_id=ep_id, status="degraded",
                              ok=False, summary=f"fold statements refused: {unsupported}"[:200])
                except Exception:
                    pass
            return ingest_knowledge_products(ep_id, statements,
                                             replay_key=transaction_fold_id(episode),
                                             derived_from=_derivation_refs(derivation),
                                             origin_episode_id=str(derivation["episode_id"]),
                                             unsupported=unsupported)

        with phase_span("pressure", "knowledge_ingest", episode_id=ep_id) as span:
            # FENCED (review 2026-09-17): ownership of the mail this fold reasoned over is
            # proven and the intake journalled in ONE critical section of the mail store. A
            # check made beforehand let the lease run out in between, leaving durable
            # records derived from words another fold now owned.
            receipts = while_omega_mail_owned(episode, accept)
            span.set(
                statements=len(receipts),
                committed=sum(1 for r in receipts if r.get("status") == "committed"),
            )
    # A refused or unsupported fold accepted nothing, so it edges nothing.
    _record_fold_causal_facts(episode, global_, receipts)
    knowledge_receipts = [r for r in receipts if r.get("kind") not in _DELTA_KINDS]
    delta_receipts = [r for r in receipts if r.get("kind") in _DELTA_KINDS]
    if knowledge_receipts:
        omega_dict["knowledge_receipts"] = knowledge_receipts
    if delta_receipts:
        omega_dict["delta_receipts"] = delta_receipts
    if any(row.get("accepted") for row in knowledge_receipts):
        return
    # No ACCEPTED knowledge: the no-new-learning cap below applies. The presence of receipts
    # is not acceptance — a fold whose only statement was refused (nothing journalled) kept
    # its proposed 0.9 over a prior 0.2 (review 2026-09-17). The refusal receipts stay on the
    # carrier above, as the evidence of why nothing was learned.
    effects = omega_dict.get("mastery_effects")
    if not isinstance(effects, (list, tuple)):
        return
    packet = global_.get("omega_packet")
    prior = packet.get("prior_mastery") if isinstance(packet, dict) else None
    prior_domain = str(packet.get("domain") or "") if isinstance(packet, dict) else ""
    try:
        prior_knowledge = float(
            prior.get("knowledge_mastery") or 0.0 if isinstance(prior, dict) else 0.0
        )
    except (TypeError, ValueError):
        prior_knowledge = 0.0
    gated: list = []
    capped = 0
    for eff in effects:
        if isinstance(eff, dict):
            try:
                claimed = float(eff.get("knowledge_mastery") or 0.0)
            except (TypeError, ValueError):
                claimed = prior_knowledge
            effect_domain = str(eff.get("domain") or "")
            ceiling = prior_knowledge if not prior_domain or effect_domain == prior_domain else 0.0
            # MasteryEffect carries an ABSOLUTE level, not a delta. With no new
            # receipts an honest downgrade remains valid, but an increase is
            # capped at the prior level. Setting it to zero would erase already
            # earned mastery after a no-new-learning episode.
            if claimed > ceiling:
                eff = {**eff, "knowledge_mastery": ceiling}
                capped += 1
        gated.append(eff)
    if capped:
        omega_dict["mastery_effects"] = gated
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "knowledge_mastery_capped",
                episode_id=ep_id,
                summary=(
                    f"capped {capped} knowledge_mastery level(s) at prior state: "
                    "fold claimed an increase with no statements or receipts"
                ),
                status="ok",
                capped=capped,
            )
        except Exception:
            pass


def _apply_motor_loop_guard(
    program,
    evidence,
    prior_attempts,
    successor,
    *,
    episode_id: str,
    episode=None,
):
    """Apply a factual loop decision to the next pressure/ask boundary.

    ``episode`` (Phase 4 cut 9 review) receives the attempt history INCLUDING
    the attempt this close just minted, under
    ``motor_loop_guard.ATTEMPTS_THROUGH_CLOSE_KEY``. Until cut 9 the appended
    list only ever left this function on the successor pressure, which is
    enough while every continuation of a build re-enters through that
    successor. A durable job continuation instead carries the history by
    VALUE onto a fresh episode, and reading the planner's pre-close
    ``prior_motor_attempts`` there means each hop re-arrives with the history
    the PREVIOUS episode planned against — so the equivalent/distinct limits
    count the same attempts forever and only MAX_CHAIN_PASSES bounds the loop.
    Deliberately NOT written to ``prior_motor_attempts``: the journal reads that
    key as the attempts that preceded this one to form its RPE expectation, and
    folding this episode's own attempt into it would corrupt that baseline."""
    from collections.abc import Mapping
    from copy import deepcopy

    from pressure_loop.contributors import make_contributor
    from pressure_loop.motor_loop_guard import (
        ATTEMPTS_THROUGH_CLOSE_KEY,
        evaluate_motor_loop,
        material_pathway_fingerprint,
        material_pathway_signature,
    )
    from pressure_loop.policy import (
        motor_programs_distinct_failure_limit,
        motor_programs_equivalent_no_progress_limit,
    )
    from pressure_loop.pressure import stage_reseed

    prior_rows = [dict(row) for row in prior_attempts if isinstance(row, Mapping)]
    successor_action = (
        dict(successor.get("proposed_action") or {}) if isinstance(successor, dict) else {}
    )
    decision = evaluate_motor_loop(
        program,
        evidence,
        prior_rows,
        equivalent_limit=motor_programs_equivalent_no_progress_limit(),
        distinct_limit=motor_programs_distinct_failure_limit(),
        proposed_required_capabilities=tuple(successor_action.get("required_capabilities") or ()),
    )
    if decision.action == "ask_operator":
        methods = ", ".join(decision.attempted_methods) or "the attempted pathways"
        contradiction = decision.unresolved_contradiction or "the unresolved evidence conflict"
        question = (
            f"I tried {methods}, but {contradiction} remains unresolved. "
            "Which constraint or factual premise should I use to proceed?"
        )
        stage_reseed(
            [
                make_contributor(
                    "omega",
                    "operator_priority",
                    1.0,
                    {
                        "question": question,
                        "message": question,
                        "episode_id": str(episode_id or ""),
                        "outcome_id": str(getattr(program, "outcome_id", "") or ""),
                        "attempted_methods": list(decision.attempted_methods),
                        "unresolved_contradiction": contradiction,
                        "motor_loop_decision": decision.to_dict(),
                    },
                )
            ]
        )
        return decision, None

    revised = deepcopy(successor) if isinstance(successor, dict) else None
    if decision.distinct_failures <= 0:
        return decision, revised

    pathway = getattr(program, "pathway", None)
    method = str(getattr(pathway, "method", None) or decision.failed_fingerprint or "").strip()
    execution_plan = getattr(program, "execution_plan", None)
    capability = str(getattr(execution_plan, "capability_class", None) or "").strip()
    required_capabilities = tuple(
        str(value).strip()
        for value in getattr(execution_plan, "required_capabilities", ())
        if str(value).strip()
    )
    normalized_evidence = dict(decision.to_dict().get("execution_evidence") or {})
    attempt = {
        "program_id": str(getattr(program, "program_id", "") or ""),
        "outcome_id": str(getattr(program, "outcome_id", "") or ""),
        "program_fingerprint": str(program.fingerprint()),
        "material_fingerprint": material_pathway_fingerprint(program),
        "material_signature": material_pathway_signature(program),
        "verified_progress": False,
        "failure_kind": str(normalized_evidence.get("failure_kind") or ""),
        "execution_status": str(normalized_evidence.get("status") or evidence.status or ""),
        "method": method,
        "required_capabilities": list(
            dict.fromkeys(([capability] if capability else []) + list(required_capabilities))
        ),
        "loop_action": decision.action,
        "capability_exception_used": decision.capability_exception_used,
        "requires_material_change": decision.requires_material_change,
        "outstanding_required_capabilities": list(decision.outstanding_required_capabilities),
        "execution_evidence": normalized_evidence,
    }
    carried_attempts = [*prior_rows, attempt][-12:]
    # The post-close history, for the one reader that leaves this episode by
    # value instead of by successor pressure (the durable job continuation).
    if episode is not None:
        live = getattr(episode, "meta", None)
        if not isinstance(live, dict):
            live = dict(live or {})
            episode.meta = live
        live[ATTEMPTS_THROUGH_CLOSE_KEY] = [dict(row) for row in carried_attempts]
    if revised is not None:
        proposed_action = dict(revised.get("proposed_action") or {})
        proposed_action["prior_attempts"] = carried_attempts
        if decision.action == "invalidate":
            proposed_action["failed_program_fingerprint"] = decision.failed_fingerprint
            proposed_action["failed_execution_evidence"] = evidence.to_dict()
        if decision.requires_material_change:
            proposed_action["failed_material_fingerprint"] = decision.failed_material_fingerprint
            proposed_action["outstanding_required_capabilities"] = list(
                decision.outstanding_required_capabilities
            )
        revised["proposed_action"] = proposed_action
    elif decision.action == "invalidate":
        raise RuntimeError("invalidated motor pathway requires a successor pressure")
    return decision, revised


def _persist_motor_attempt_and_pressure(episode, result) -> None:
    """Close an Omega-authored attempt in evidence-first continuity order.

    This deliberately does not update mastery, promote a pathway, or assign
    pathway credit. DeepSleep owns those later evidence-gated transitions.
    """
    meta = dict(getattr(episode, "meta", None) or {})
    raw_program = meta.get("motor_program")
    if raw_program is None or (isinstance(raw_program, dict) and raw_program.get("shadow") is True):
        return
    # A dispatching episode reaches the `raw_program is None` return above: the
    # handoff MOVES the program to the agent line rather than copying it (cut
    # 9), precisely so that this close — which would read a skipped AUTO as
    # status "failure" and mint a phantom failed attempt against the pathway —
    # never has a program to close for work that has not run yet.
    from auto_loop.motor_adapter import execution_evidence_from_fold
    from memory.motor_experience import motor_attempt_identity, persist_attempt_sequence
    from omega_loop.episode.prompts import _parse_pressure_lifecycle
    from omega_loop.episode.run import _packet_from_episode
    from pressure_loop.emergent_pressure import (
        create_emergent_pressure,
        resolve_emergent_pressure,
        stage_emergent_pressure,
    )
    from schema.emergent_pressure import EmergentPressure
    from schema.motor_program import MotorProgram

    program = (
        raw_program
        if isinstance(raw_program, MotorProgram)
        else MotorProgram.from_dict(raw_program)
    )
    packet = _packet_from_episode(episode)
    if packet is None or not isinstance(packet.motor_program, dict):
        raise ValueError("motor attempt close requires its canonical Omega packet")
    if packet.motor_program != program.to_dict():
        raise ValueError("Omega packet motor program conflicts with the attached program")
    resolution, successor = _parse_pressure_lifecycle(
        {
            "pressure_resolution": getattr(result, "pressure_resolution", None),
            "emergent_pressure": getattr(result, "emergent_pressure", None),
        },
        packet,
    )
    originating_pressure = (
        EmergentPressure.from_dict(packet.originating_pressure)
        if isinstance(packet.originating_pressure, dict)
        else None
    )
    evidence = execution_evidence_from_fold(episode)
    identity = motor_attempt_identity(program, evidence)
    execution_pack = dict(meta.get("execution_action_pack") or {})
    execution_inputs = dict(execution_pack.get("execution_inputs") or {})
    pressure_input = dict(execution_inputs.get("emergent_pressure") or {})
    proposed_action = dict(pressure_input.get("proposed_action") or {})
    prior_attempts = (
        execution_inputs.get("prior_attempts")
        or proposed_action.get("prior_attempts")
        or meta.get("prior_motor_attempts")
        or meta.get("prior_attempts")
        or ()
    )
    from pressure_loop.motor_loop_guard import (
        evaluate_motor_loop,
        material_pathway_fingerprint,
        material_pathway_signature,
    )
    from pressure_loop.policy import (
        motor_programs_distinct_failure_limit,
        motor_programs_equivalent_no_progress_limit,
    )

    successor_action = (
        dict(successor.get("proposed_action") or {}) if isinstance(successor, dict) else {}
    )
    loop_decision = evaluate_motor_loop(
        program,
        evidence,
        prior_attempts,
        equivalent_limit=motor_programs_equivalent_no_progress_limit(),
        distinct_limit=motor_programs_distinct_failure_limit(),
        proposed_required_capabilities=tuple(successor_action.get("required_capabilities") or ()),
    )
    assessment = {
        "episode_assessment": dict(getattr(result, "episode_assessment", None) or {}),
        "what_changed": dict(getattr(result, "what_changed", None) or {}),
        "synthesis": getattr(result, "synthesis", None),
        "reflection": getattr(result, "reflection", None),
        "execution_status": evidence.status,
        "program_fingerprint": program.fingerprint(),
        "material_fingerprint": material_pathway_fingerprint(program),
        "material_signature": material_pathway_signature(program),
        "motor_loop_decision": loop_decision.to_dict(),
    }
    resolution = dict(resolution or {})
    receipts = persist_attempt_sequence(
        program, evidence, assessment, resolution, identity=identity
    )
    provenance_refs = (
        [str(receipts[0].path)] if receipts and getattr(receipts[0], "path", None) else []
    )

    resolution_status = str(resolution.get("status") or "") if resolution else ""
    if resolution and resolution_status != "create":
        if originating_pressure is None:
            raise ValueError("pressure resolution requires the originating pressure")
        resolve_emergent_pressure(
            str(resolution.get("pressure_id") or ""),
            resolution,
            identity,
            originating_pressure=originating_pressure,
            provenance_refs=provenance_refs,
        )
    loop_decision, successor = _apply_motor_loop_guard(
        program,
        evidence,
        prior_attempts,
        successor,
        episode_id=str(getattr(episode, "episode_id", "") or ""),
        episode=episode,
    )
    live_meta = getattr(episode, "meta", None)
    if isinstance(live_meta, dict):
        live_meta["motor_loop_decision"] = loop_decision.to_dict()
    if successor is not None:
        pressure = EmergentPressure.from_dict(successor)
        if originating_pressure is None:
            if resolution_status != "create":
                raise ValueError("originless successor pressure requires create lifecycle")
            create_emergent_pressure(
                pressure,
                identity,
                provenance_refs=provenance_refs,
            )
        else:
            stage_emergent_pressure(
                pressure,
                identity,
                originating_pressure=originating_pressure,
                provenance_refs=provenance_refs,
            )


def _battery_red_set(verdict) -> frozenset | None:
    """The battery's RED check NAMES as a comparable set — the basis of the stall delta. Names
    only: detail text embeds volatile counts/file lists, and churn there must not reset the
    stall (a build genuinely progressing shows up as the mind DRIVING — APPLY with a directive —
    which never stall-stops). None when there is no snapshot to compare (never a stall)."""
    if not isinstance(verdict, dict):
        return None
    return frozenset(
        str(c.get("name") or "")
        for c in (verdict.get("checks") or [])
        if isinstance(c, dict) and not c.get("ok")
    )


_NOTICE_SENTENCE_RE = re.compile(r"[.!?](\s|$)")


def _compose_build_notice_text(
    *,
    objective="",
    original_request="",
    summary="",
    files=(),
    stopped_reason=None,
) -> str:
    """One short human sentence for chat AND TTS. Built from the STICKY original_request
    (clean) — the stash objective can arrive doubled (pre-fix stashes persist across
    upgrades). Falls back to the objective's FIRST sentence when no request exists."""
    base = str(original_request or "").strip() or str(objective or "").strip()
    m = _NOTICE_SENTENCE_RE.search(base)
    head = base[: m.start() + 1].strip() if m else base[:160].strip()
    if stopped_reason is not None:
        return f"⛔ Stopped: {head} {str(stopped_reason or '').strip()[:300]}".strip()
    msg = f"✅ Finished: {head}"
    summ = str(summary or "").strip()
    if summ:
        msg += f" {summ[:200]}"
    names = [str(f).replace("\\", "/").rsplit("/", 1)[-1] for f in (files or ())][:8]
    if names:
        msg += f" Files: {', '.join(names)}."
    return msg


# _persist_notice_to_chat_history RETIRED (operator 2026-07-12): notices were
# double-written into the chat-history ring and replayed into the main chat on
# every boot — "cluttered with stuff." The chat panel is now IN-SESSION ONLY
# (ring cleared at boot); durable copies of proactive questions/results/finished
# work live in their own panels and stores (system messages, agent activity,
# proactive pinboard, workflow memory).
def _persist_notice_to_chat_history(text: str) -> None:
    del text  # retired no-op kept for call-site compatibility


def _emit_build_stopped_to_operator(
    episode, objective, reason, origin_opened_by="", ported_to=""
) -> None:
    """Tell the operator their build STOPPED without completing (stall / no-evidence cap) — a
    terminal exit must never be silent for an operator-requested build. Failure-soft."""
    try:
        effective = str(origin_opened_by or "").strip()
        if not effective.startswith("operator"):
            return
        from pressure_loop.proactive_chat_messages import append_update

        msg = _compose_build_notice_text(objective=objective, stopped_reason=reason)
        if str(ported_to or "").strip():
            msg += f"\n📁 Partial work preserved at: {str(ported_to).strip()}"
        append_update(
            text=msg,
            episode_id=_episode_id(episode),
            opened_by=effective,
            intent="PROACTIVE_RESPONSE",
        )
        _persist_notice_to_chat_history(msg)
        _mark_operator_notice_posted(episode)
    except Exception:
        log.exception("build-stopped operator notice failed (non-fatal)")


def _mark_operator_notice_posted(episode) -> None:
    """Record that this episode ALREADY told the operator how its build ended.

    The durable job completion receipt on the system-messages rail exists
    for every path the deterministic build lifecycle leaves silent — a yield, a
    partial, an episode with no execution report at all. It must never double up
    on the notice the lifecycle just posted, which is the richer message and
    wins; so the lifecycle stamps the episode and the receipt reads the stamp.
    """
    meta = getattr(episode, "meta", None)
    if not isinstance(meta, dict):
        meta = dict(meta or {})
        episode.meta = meta
    meta["operator_build_notice_posted"] = True


# Omega PRESSURE with one of these intents continues a build through a successor episode.
_BUILD_CONTINUATION_INTENTS = frozenset({"USE_SKILL", "USE_TOOL", "PURSUE_OUTCOME"})


_GATE_CONTINUES_CAP = 4  # deterministic continues WITHOUT fresh fold evidence -> stop


def _build_holds_for_passoff(origin: Any, files_changed: Any) -> bool:
    """Whether a battery-verified build waits in the workspace for the MIND's pass-off before it
    ports to completed (operator 2026-07-22). True only for an AUTONOMOUS build (the mind drives
    it and must be able to SEE its finished work, else it rebuilds what it can't see) that has
    DELIVERABLES to show. An OPERATOR build ports + notifies immediately — the operator is the
    authority for their own request and there is no autonomous rebuild loop to break. A fileless
    (verify-only) run has nothing to hold."""
    return bool(files_changed) and not str(origin or "").startswith("operator")


def _apply_build_report_decision(episode, result) -> None:
    """Finalize, stop, or leave open the build represented by this episode's SRAM report."""
    from dataclasses import replace

    from memory.execution_report import current_execution_report, hold_execution_report

    report = current_execution_report()
    if report is None:
        return
    control = getattr(result, "control_decision", None)
    control = control if isinstance(control, dict) else {}
    pressure = control.get("pressure") if isinstance(control.get("pressure"), dict) else {}
    intent = str(pressure.get("intent") or "").strip().upper()
    # A DONE carries its account in synthesis; unresolved decisions carry a pressure reason.
    reason = str(pressure.get("reason") or control.get("synthesis") or "").strip()
    if control.get("decision") == "ASK_OPERATOR":
        return  # the build waits for the operator's answer
    if control.get("decision") == "PRESSURE" and intent in _BUILD_CONTINUATION_INTENTS:
        return  # a successor episode continues the build
    verification = dict(report.verification or {})
    green = (
        bool(verification.get("all_green"))
        if verification
        else bool(dict(report.metadata or {}).get("objective_complete"))
    )
    accepted = getattr(result, "build_accepted", None)
    accepted = green if accepted is None else bool(accepted)
    from core.workspace_port import record_port

    if green and accepted:
        practice_domain = str(dict(report.metadata or {}).get("practice_domain") or "").strip()
        accepted_metadata = dict(report.metadata or {})
        if practice_domain:
            accepted_metadata["practice_grounding_pending"] = True
        hold_execution_report(
            replace(
                report,
                omega_intent=intent or "ACCEPT",
                omega_reason=reason or "Omega accepted the verified build.",
                metadata=accepted_metadata,
            )
        )
        destination = record_port(
            "completed",
            episode_id=_episode_id(episode),
            objective=report.build_task,
            origin_opened_by=report.origin_opened_by,
            original_request=report.original_request,
        )
        _emit_build_complete_to_operator(
            episode,
            report.build_task,
            {
                "summary": report.summary,
                "progress_digest": report.progress_digest,
                "files_changed": report.files_changed,
            },
            origin_opened_by=report.origin_opened_by,
            original_request=report.original_request,
            ported_to=str(destination),
        )
        return
    hold_execution_report(
        replace(
            report,
            omega_intent=intent or "STOP",
            omega_reason=reason or "Omega did not accept the build.",
        )
    )
    destination = record_port(
        "stopped",
        episode_id=_episode_id(episode),
        objective=report.build_task,
        origin_opened_by=report.origin_opened_by,
        original_request=report.original_request,
    )
    unresolved = "; ".join(report.failed or report.did_not) or "Omega stopped the build."
    _emit_build_stopped_to_operator(
        episode,
        report.build_task,
        unresolved,
        origin_opened_by=report.origin_opened_by,
        ported_to=str(destination),
    )


def _refresh_packet_wip_from_execution_report(episode) -> None:
    """Inject this episode's SRAM execution report into Omega's already-built packet."""
    from memory.execution_report import current_execution_report

    report = current_execution_report()
    if report is None:
        return
    global_ = getattr(episode, "global_", None)
    if not isinstance(global_, dict):
        raise RuntimeError("execution report exists but Global B pack is missing")
    packet = global_.get("omega_packet")
    if not isinstance(packet, dict):
        raise RuntimeError("execution report exists but Omega packet is missing")
    payload = report.to_payload()
    row = {
        "objective": report.build_task,
        "original_request": report.original_request,
        "pass_gate": list(report.pass_gate),
        "progress_digest": report.progress_digest,
        "current_state": report.summary,
        "files_changed": list(report.files_changed),
        "test_results": list(report.succeeded),
        "errors": list(report.failed),
        "open_questions": list(report.did_not),
        "iterations": report.rounds_used,
        "rounds_used": report.rounds_used,
        "verification": dict(report.verification or {}),
        "execution_report": payload,
    }
    prior = [item for item in (packet.get("build_in_progress") or ()) if isinstance(item, dict)]
    packet["build_in_progress"] = [row] + [
        item for item in prior if str(item.get("objective") or "") != report.build_task
    ]
    packet["execution_report"] = payload


def _episode_origin_opened_by(episode) -> str:
    """The provenance that decides whether this episode's work is the OPERATOR's.

    A durable job continuation may be opened by ``agent_motor_dispatch``,
    a machine-minted type — but the build it runs is the operator's, planned in
    their chat episode moments earlier. Two deterministic readers ask this
    question and would both regress if they read the raw provenance: the
    workspace port sends non-operator work to ``_practice`` instead of the
    operator's completed directory, and the build-complete/stopped notices fire
    only for an operator origin. The carried origin is therefore authoritative
    for that continuation; every other episode answers with its own provenance,
    exactly as before.
    """
    from pressure_loop.agent_lane import carried_origin_opened_by

    carried = carried_origin_opened_by(episode)
    if carried:
        return carried
    return str(
        getattr(episode, "opened_by", None)
        or (dict(getattr(episode, "pressure", None) or {})).get("opened_by")
        or ""
    )


def _operator_request_text(episode) -> str:
    """The operator's VERBATIM message that opened this episode.

    System-event contributors (rollback, etc.) can carry diagnostic messages but are not the
    operator request the build battery should verify against.

    A durable job continuation carries the operator's verbatim ask forward from the
    chat episode that planned the build (cut 9) — the build battery verifies
    against what the OPERATOR asked for, never against the plan's restatement of
    it, so the carried request wins over anything readable off this episode's
    own machine-minted contributor.
    """
    from pressure_loop.agent_lane import carried_original_request

    carried = carried_original_request(episode)
    if carried:
        return carried
    try:
        pressure = dict(getattr(episode, "pressure", None) or {})
        rows = [row for row in pressure.get("contributors") or [] if isinstance(row, dict)]
        for wanted in (
            "operator_query",
            "operator_command",
            "operator_response",
            "operator_message",
        ):
            for row in rows:
                if str(row.get("id") or row.get("type") or "") != wanted:
                    continue
                meta = dict(row.get("metadata") or row.get("detail") or {})
                candidate = str(meta.get("question") or meta.get("message") or "").strip()
                if candidate:
                    return candidate
        for row in rows:
            if str(row.get("id") or row.get("type") or "") == "dev_rollback":
                continue
            meta = dict(row.get("metadata") or row.get("detail") or {})
            candidate = str(meta.get("question") or meta.get("message") or "").strip()
            if candidate:
                return candidate
    except Exception:
        pass
    return ""


def _record_signal_omega(episode, result) -> None:
    """Failure-soft Step-8 SignalEvent for the per-episode Omega result. Honest fields
    only — confidence from the prepared intent_decision when present, else None; never
    fabricated. Runs at episode close, never on the hot tick; never raises."""
    try:
        from pressure_loop.signal_ledger import SignalEvent, record

        decision = getattr(result, "intent_decision", None) or {}
        confidence = decision.get("confidence") if isinstance(decision, dict) else None
        meta = {"cognized": bool(getattr(result, "cognized", False))}
        intent = decision.get("intent") if isinstance(decision, dict) else None
        if intent:
            meta["prepared_intent"] = str(intent)
        record(
            SignalEvent(
                source="omega_result",
                kind="outcome",
                episode_id=str(getattr(result, "episode_id", "") or ""),
                domain=_resolve_episode_domain(episode, result),
                confidence=confidence,
                meta=meta,
            )
        )
    except Exception:
        log.exception("omega signal record failed (non-fatal)")


def _resolve_episode_domain(episode, result):
    """The episode's domain: omega_packet.domain -> mastery_effects[0].domain -> None.
    Pure .get()/guarded access — a degraded Global B may leave no omega_packet, and a
    bracket subscript would KeyError. Shared by the pressure + intent shadow writes so
    both setpoints key to the SAME domain per episode."""
    global_ = getattr(episode, "global_", None)
    packet = global_.get("omega_packet") if isinstance(global_, dict) else None
    domain = packet.get("domain") if isinstance(packet, dict) else None
    if not domain:
        effects = getattr(result, "mastery_effects", None) or ()
        if effects:
            domain = getattr(effects[0], "domain", None)
    return domain


def _record_trajectory_influence(episode, result) -> None:
    """Failure-soft shadow write of result.trajectory_influence into the window-influence store
    (2026-06-30). Runs every episode close; never raises, never mutates episode.global_. The
    10-episode window reads these to author its trajectory — pressure is untouched (decouple)."""
    try:
        from pressure_loop import trajectory_influence_store

        influence = getattr(result, "trajectory_influence", None)
        if not influence:
            return
        trajectory_influence_store.record_influence(
            influence,
            episode_id=str(getattr(result, "episode_id", "") or ""),
            domain=_resolve_episode_domain(episode, result),
        )
    except Exception:
        log.exception("trajectory influence shadow-write failed (non-fatal)")


# ``_apply_per_episode_mastery_delta`` was REMOVED (risk 8, ruling 2026-08-11).
# See the terminus above: the delta now rides
# ``EpisodeStore.commit_reflection_window_append`` from inside the serialized
# commit region, alongside the body it describes. This function ran AFTER that
# region under one pipeline lock alone and annotated whichever window the store was
# collecting at that instant, which under four live pipelines is not reliably the
# window the episode joined.


def _record_phase_failure(capture, *, producer_error: BaseException) -> None:
    """Record a raised phase failure, keeping BOTH causes visible if the record itself fails.

    The producer's error is the one that must reach the caller: it is what actually went wrong.
    So a capture failure is never allowed to replace it — it is chained to it instead, which keeps
    the second fault diagnosable without hiding the first (AGENTS.md section 2.1).
    """
    try:
        capture()
    except Exception as capture_error:  # noqa: BLE001 - re-raised below with both causes
        log.exception("could not record the failed phase; the producer error still propagates")
        # WO-04A closure finding F4. `raise producer_error from capture_error` OVERWRITES
        # `producer_error.__cause__`, so a producer that already wrapped its own substrate failure
        # lost that root cause — the caller could no longer reach what actually went wrong.
        #
        # Instead: leave `__cause__` alone and let implicit chaining attach the recording failure as
        # `__context__`, so the producer stays primary with its own root cause and the recording
        # failure stays inspectable.
        #
        # r2 then did this UNCONDITIONALLY, which is closure finding R3-2: it assumed the recording
        # error's context could only be the automatic edge back to the producer. A recording
        # subsystem that catches one failure and raises another has its OWN context there, and
        # overwriting it destroyed that second root. The edge is only re-pointed when it really is
        # the automatic self-reference that would otherwise form a cycle.
        if capture_error.__context__ is producer_error:
            # Python set this because the producer was the exception being handled. Re-point it at
            # the producer's own prior context — which implicit chaining is about to displace —
            # so nothing is dropped and no cycle forms.
            capture_error.__context__ = producer_error.__context__
        raise producer_error


def _start_global_a(store, episode, ignition, cascade_status: str = "completed") -> bool:
    """Global A: ignition → IntentDecision → ignite Meta A. Returns False when
    run_global_a could not produce an IntentDecision (cascade failure)."""
    ep_id = _episode_id(episode)
    from memory.cognition_capture import capture_intent_selected, new_invocation_id

    # ONE identity for THIS invocation of the producer (WO-04A closure R3-1). A failure recorded
    # under it is that invocation's occurrence; running the producer again is a new invocation and
    # therefore a new occurrence, however its error happens to read.
    invocation = new_invocation_id()
    try:
        with phase_span("pressure", "global_a_handoff", episode_id=ep_id):
            from global_loop.loop import run_global_a

            decision = run_global_a(episode, ignition)
    except Exception as exc:
        # WO-04A finding A. A RAISED phase failure is evidence too, and it must be on record
        # before the exception leaves this handoff. The original exception is then re-raised
        # unchanged, so the producer's diagnostic is what propagates and Meta A never runs.
        # The reason is built HERE because `except ... as exc` unbinds the name at the end of the
        # block, so a closure over it would be reading a name that no longer exists.
        reason = f"Global A raised {type(exc).__name__}: {exc}"
        _record_phase_failure(
            lambda: capture_intent_selected(episode, None, route=None, failure_reason=reason,
                                            invocation_id=invocation),
            producer_error=exc,
        )
        raise
    if decision is None:
        # Record the absence BEFORE raising, so a failed phase is not an unrecorded one — then
        # raise, so the next phase does not run and the original failure is not softened. Recorded
        # as a FAILURE, not as a selection of nothing: a phase that decided nothing must not enter
        # the decision lineage (WO-04A closure finding F1).
        capture_intent_selected(
            episode, None, route=None,
            failure_reason="Global A produced no IntentDecision",
            invocation_id=invocation,
        )
        raise RuntimeError("Global A produced no IntentDecision — invalid cognitive state")
    # WO-04. Global A's COMPLETION is captured here, at the cascade handoff, because this is where
    # the episode's owner is guaranteed: the admission seam opened its SRAM bucket. `run_global_a`
    # itself stays a pure phase function with no memory concern, which is also why its own unit
    # tests need no bucket. The decision recorded is the real one this phase just returned.
    capture_intent_selected(episode, decision, route=_operator_route_of(episode))
    pace_autonomous_phase(episode, "meta_a")
    _start_meta_a(store, episode, cascade_status)
    return True


def _operator_route_of(episode):
    """The route object Global A resolved for this episode, or None when it routed nothing.

    Rebuilt from the episode's own ``global_["operator_route"]`` rather than returned out of the
    phase, so the capture reads what Global A actually WROTE and no signature changes.
    """
    routed = dict(getattr(episode, "global_", None) or {}).get("operator_route")
    return routed if isinstance(routed, dict) and routed else None


def _start_meta_a(store, episode, cascade_status: str = "completed") -> None:
    """Meta A: IntentDecision → ExecutionActionPack → ignite AUTO."""
    ep_id = _episode_id(episode)
    # WO-04, at the handoff for the same reason as Global A's: the owner is guaranteed here. An
    # absent pack is recorded as absent and then raises, so the contract phase does not run.
    from memory.cognition_capture import (
        META_A_FAILED,
        capture_action_pack,
        capture_phase_failure,
        new_invocation_id,
    )

    # One identity for THIS invocation of Meta A (WO-04A closure R3-1).
    invocation = new_invocation_id()
    try:
        with phase_span("pressure", "meta_a_handoff", episode_id=ep_id):
            from meta_loop.loop import run_meta_a

            pack = run_meta_a(episode)
    except Exception as exc:
        # WO-04A finding A, Meta A's half. The reason is built here for the same scoping reason.
        # Recorded under the FAILURE token, not the pack's: reusing `meta-a-pack:{episode}` after a
        # successful pack made the accepted event API refuse the write, so a later failure recorded
        # nothing at all (WO-04A closure finding F1).
        reason = f"Meta A raised {type(exc).__name__}: {exc}"
        _record_phase_failure(
            lambda: capture_phase_failure(
                episode, phase="meta_a", event_kind=META_A_FAILED, reason=reason,
                invocation_id=invocation,
            ),
            producer_error=exc,
        )
        raise
    if pack is None:
        capture_phase_failure(
            episode, phase="meta_a", event_kind=META_A_FAILED,
            reason="Meta A produced no ExecutionActionPack",
            invocation_id=invocation,
        )
        raise RuntimeError("Meta A produced no ExecutionActionPack — invalid cognitive state")
    capture_action_pack(episode, pack)
    _bind_execution_contract(store, episode, cascade_status)


def _bind_execution_contract(store, episode, cascade_status: str = "completed") -> None:
    """Execution contract: ExecutionActionPack -> MotorProgram -> AUTO, with no inference.

    Meta A's validated pack is projected into the attempt's canonical contract, or a
    carried agent episode rebinds the program its parent already bound. Every
    episode then proceeds directly to AUTO on its already leased physical pipeline.
    There is no chat-to-agent handoff and no acknowledgement response: AUTO executes
    the actual action and its grounded result is what resolves the operator's HUD turn.
    """
    ep_id = _episode_id(episode)
    from memory.cognition_capture import (
        CONTRACT_FAILED,
        capture_phase_failure,
        new_invocation_id,
    )

    # One identity for THIS invocation of the contract binder (WO-04A closure R3-1).
    invocation = new_invocation_id()
    try:
        with phase_span("pressure", "execution_contract", episode_id=ep_id) as span:
            from meta_loop.execution_contract import bind_execution_contract

            attached = bind_execution_contract(episode)
            span.set(
                contracted=attached is not None,
                program_id=str((attached or {}).get("program_id") or ""),
            )
    except Exception as exc:
        # WO-04A closure finding F1, the adjacent boundary. `bind_execution_contract` converts its
        # OWN declared-terms failure into an uncontracted outcome, but a substrate mismatch raises
        # a plain error straight through — and that reached the caller with no record at all. AUTO
        # is already correctly refused; the evidence was the missing half.
        reason = f"the execution contract raised {type(exc).__name__}: {exc}"
        _record_phase_failure(
            lambda: capture_phase_failure(
                episode, phase="meta_a", event_kind=CONTRACT_FAILED, reason=reason,
                invocation_id=invocation,
            ),
            producer_error=exc,
        )
        raise
    # WO-04. THE BAR BEFORE THE WORK. This is the last statement before `_start_auto`, so the
    # acceptance criteria are durable before the first action can run, and WO-03's sealed
    # occurrence makes them immutable afterwards: they cannot be moved to fit a result.
    # The status/reason come from the owner that made the decision, via its own outcome note.
    from memory.cognition_capture import capture_acceptance_criteria
    from meta_loop.execution_contract import CONTRACT_OUTCOME_KEY

    _outcome = dict(dict(getattr(episode, "meta", None) or {}).get(CONTRACT_OUTCOME_KEY) or {})
    _contract = dict(attached or {})
    _outcome_contract = dict(_contract.get("outcome_contract") or {})
    capture_acceptance_criteria(
        episode,
        objective=str(_outcome_contract.get("objective") or "") or None,
        criteria=list(_outcome_contract.get("acceptance_criteria") or ()),
        status=str(_outcome.get("status") or ("contracted" if attached else "uncontracted")),
        reason=str(_outcome.get("reason") or ""),
        program_id=str(_contract.get("program_id") or ""),
        criteria_revision=dict(_contract.get("pathway") or {}).get("revision"),
    )
    pace_autonomous_phase(episode, "auto")
    _start_auto(store, episode, cascade_status)


_FETCH_COMPREHEND_MAX_ROWS = 5
# Full-article comprehension width (2026-07-19): web_fetch now delivers article
# BODIES (~24k chars), not lead paragraphs — the old 1500 cap fed the
# comprehension ~300 usable chars and the mind "learned" intros.
_FETCH_COMPREHEND_MAX_CONTENT = 8000


def _ground_fetched_sources(material, proposed) -> list[dict[str, str]] | None:
    """Bind model-supplied names to the exact URLs returned by web_fetch.

    The model may name a source whose fetch row lacked a title, but it may not
    invent, replace, or omit provenance. Every fetched URL must survive and have
    a name before the learned prose can become canonical memory.
    """
    proposed_rows = proposed if isinstance(proposed, list) else []
    names_by_url: dict[str, str] = {}
    for row in proposed_rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        name = str(row.get("name") or "").strip()
        if url and name:
            names_by_url[url] = name[:200]

    grounded: list[dict[str, str]] = []
    for row in material:
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        fetched_name = str(row.get("title") or "").strip()
        name = fetched_name or names_by_url.get(url, "")
        if not name:
            return None
        grounded.append({"name": name[:200], "url": url[:500]})
    return grounded or None


def _fetched_domain_catalog() -> list:
    """Every domain memory can file fetched knowledge under: world (2) and coding (8)."""
    from memory.dewey_map import SUBDOMAIN_MAP

    return sorted({name for (lobe, _y), name in SUBDOMAIN_MAP.items() if lobe in (2, 8)})


def _fetched_comprehension_degraded(outputs: dict, ep_id: str, failure: str) -> None:
    """Honest degraded marker — the fold sees the comprehension failed; the
    deterministic artifact (knowledge_extractor) already recorded the fetch."""
    outputs["fetched_knowledge_error"] = failure
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "fetched_comprehension_failed",
            episode_id=ep_id,
            summary=failure[:200],
            status="degraded",
            ok=False,
        )
    except Exception:
        pass


def _comprehend_and_ingest_fetched(episode) -> None:
    """Fetched material -> ONE naming/comprehension inference -> memory commit -> fold.

    Operator pipeline (2026-07-11): the content was already pulled
    deterministically (knowledge_extractor in AUTO's execute). Here one chat-model
    call names each source (adds the missing name) and writes a prose account of
    what was learned, classified world vs coding. The memory commit happens RIGHT
    AFTER the inference — receipt in hand — and only then does the cascade fold
    the comprehension through Meta B/Global B/Omega (episode.auto outputs carries
    it, plus the dedicated packet field). Routing: deterministic marker router
    decides lobe 2 (world) vs 8 (coding); model kind is a tiebreak only.

    Failure posture: no fetched rows -> no-op. Inference/parse failure -> honest
    degraded marker on outputs (the fold sees the failure), no write, no raise.
    A memory-commit failure after successful comprehension raises (fail-hard:
    memory is first and truth)."""
    auto = getattr(episode, "auto", None)
    if not isinstance(auto, dict):
        return
    execution_pack = auto.get("execution_pack")
    if isinstance(execution_pack, dict):
        tool = str(execution_pack.get("tool") or "").strip()
        if tool and tool not in {"web_fetch", "web_search"}:
            return
    outputs = auto.get("outputs")
    if not isinstance(outputs, dict):
        return
    if outputs.get("fetched_knowledge") is not None:
        return
    tool_result = outputs.get("tool_result")
    if not isinstance(tool_result, dict):
        return
    rows = tool_result.get("results")
    if not isinstance(rows, list) or not rows:
        return
    ep_id = _episode_id(episode)
    domain = str(tool_result.get("domain") or outputs.get("knowledge_domain") or "").strip()
    # A fetch with no learning domain has the comprehension choose a real home for it.
    known_domains = [] if domain else _fetched_domain_catalog()

    material = []
    for row in rows[:_FETCH_COMPREHEND_MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        material.append(
            {
                "title": str(row.get("title") or row.get("source") or "")[:200],
                "url": str(row.get("url") or "")[:500],
                "content": str(row.get("content") or row.get("text") or row.get("summary") or "")[
                    :_FETCH_COMPREHEND_MAX_CONTENT
                ],
            }
        )
    if not material:
        return

    from organ.promptorgan import PromptContext, phrase

    comprehension_request = phrase(
        PromptContext(
            kind="web_comprehension",
            question=domain or "unknown",
            data={"domain": domain, "material": material, "known_domains": known_domains},
        )
    )
    system = comprehension_request.system
    user = comprehension_request.user

    parsed = None
    failure = ""
    # Live idle learning runs on local models; a second full comprehension
    # inference can add 60-120s when the first output fails local grounding.
    # Surface that as a degraded foldback artifact instead of silently doubling
    # episode latency.
    _COMPREHEND_RETRIES = 1
    with phase_span("pressure", "fetched_comprehension", episode_id=ep_id) as span:
        for attempt in range(_COMPREHEND_RETRIES):
            attempt_user = user
            if failure:
                attempt_user += (
                    "\nREPAIR REQUIRED: your previous output failed: "
                    f"{failure[:400]}. Return a complete corrected JSON object; "
                    "do not explain the repair."
                )
            try:
                from runtime.socket import invoke_runtime_generate, pipeline_context_for_episode

                response = invoke_runtime_generate(
                    operation="knowledge_comprehension",
                    episode_id=f"{ep_id}-fetched-knowledge",
                    prompt=attempt_user,
                    system=system,
                    max_tokens=2048,
                    temperature=0.2,
                    top_p=0.9,
                    enable_thinking=False,
                    tools=None,
                    timeout=fetched_comprehension_timeout_seconds(),
                    **pipeline_context_for_episode(episode),
                )
                text = response.get("text") if isinstance(response, dict) else None
                if text:
                    from omega_loop._json_extract import extract_json_object

                    parsed = extract_json_object(str(text))
                if not isinstance(parsed, dict) or not str(parsed.get("learned") or "").strip():
                    parsed = None
                    failure = "comprehension output missing or unparseable"
                elif _ground_fetched_sources(material, parsed.get("sources")) is None:
                    parsed = None
                    failure = "comprehension sources were not grounded to every fetched URL"
                else:
                    break
            except Exception as exc:
                failure = f"comprehension inference failed: {exc}"
            if attempt + 1 < _COMPREHEND_RETRIES:
                continue
        span.set(sources=len(material), ok=parsed is not None)

    if parsed is None:
        _fetched_comprehension_degraded(outputs, ep_id, failure)
        return

    # Memory commit RIGHT AFTER inference — before any fold consumes it.
    from memory.fetched_ingest import UnfileableDomain, ingest_fetched_knowledge

    grounded_sources = _ground_fetched_sources(material, parsed.get("sources"))
    if grounded_sources is None:  # defensive: validation above owns this invariant
        raise RuntimeError("fetched source grounding invariant was lost before commit")
    try:
        receipt = ingest_fetched_knowledge(
            ep_id,
            learned=str(parsed.get("learned") or ""),
            sources=grounded_sources,
            domain=domain or str(parsed.get("domain") or "").strip(),
            kind_hint=str(parsed.get("kind") or ""),
            artifact_id=str(outputs.get("knowledge_artifact_id") or "") or None,
        )
    except UnfileableDomain as exc:
        # Nothing was written: the comprehension gave the record no home memory knows.
        # That is a comprehension shortfall the fold must see, not a lost commit, so it
        # degrades the learning action instead of shutting cognition down (live
        # 2026-09-14: a domain-less web fetch killed Omega).
        _fetched_comprehension_degraded(outputs, ep_id, f"fetched material has no filed domain: {exc}")
        return
    outputs["fetched_knowledge"] = {
        "kind": receipt.get("kind"),
        "sources": receipt.get("sources"),
        "learned": str(parsed.get("learned") or ""),
        "receipt": receipt,
    }


#: The reward-prediction expectation when nothing has been measured yet. An RPE needs an
#: expectation; with no attempted tasks in the recent journal the neutral one is used, and it is
#: labelled a PRIOR wherever it is recorded, never presented as a measured success rate.
NEUROMOD_PRIOR_EXPECTATION = 0.5


def _neuromod_expectation(population) -> tuple[float, str]:
    """The neuromod baseline from a typed outcome population: measured, or the stated prior.

    The measured value is the recent task success rate over attempted tasks — verified or not,
    because the AUTO cycle it is compared against is itself unverified. Cascade completion no
    longer counts as success anywhere in it (WO-06).
    """
    rate = population.task_success_rate
    if rate is None:
        return NEUROMOD_PRIOR_EXPECTATION, "prior"
    return float(rate), "measured"


def _start_auto(store, episode, cascade_status: str = "completed") -> None:
    """AUTO: ExecutionActionPack → AutoOutcomePack → ignite Meta B."""
    ep_id = _episode_id(episode)
    lane_origin = _specialist_lane_origin(episode)
    if lane_origin is not None:
        lane_label = "Browser" if lane_origin["lane"] == "browser" else "Coding Agent"
        _append_specialist_lane_update(
            episode,
            lane_origin,
            status="started",
            text=f"The {lane_label} lane picked up this request.",
        )
    # Motor execution means the physical pipeline is waiting on a tool, not on the
    # model, and that is the distinction the ratified design's own example draws
    # ("agent waiting on test result for episode X"). AUTO's front is where a
    # build spends its minutes, so it is where a crash is most likely to find a
    # pipeline; the state returns to INFERENCE below because Meta B and Global B are
    # model passes again and an envelope that lied about which would make the
    # restoration report useless to the operator reading it.
    _pipeline_state.mark_waiting_tool(episode)
    with phase_span("pressure", "auto_handoff", episode_id=ep_id):
        from auto_loop.auto import run_auto_front

        if run_auto_front(episode) is None:
            raise RuntimeError("AUTO produced no AutoOutcomePack — invalid cognitive state")
    _pipeline_state.mark_inference(episode)
    # The reply exists NOW — resolve the operator's bubble before Meta B / Global B /
    # Omega / DeepSleep, which fold back behind the delivered answer.
    _deliver_chat_reply_at_auto(episode)
    # Cut 11 review: the action has EXECUTED and the answer is out, so this turn
    # is no longer owed and must never be re-armed by a boot restore — a replay
    # from here would re-generate the image, re-write the document, re-ingest the
    # fetched page and re-answer a question the operator already has an answer
    # to. See core.pipeline_state.mark_acted.
    _pipeline_state.mark_acted(episode)
    # FETCHED-KNOWLEDGE COMPREHENSION (operator pipeline 2026-07-11): content was
    # pulled deterministically in AUTO's execute; ONE inference names the sources +
    # writes what was learned; the memory commit lands IMMEDIATELY after inference
    # (receipt in hand), and only THEN does the outcome fold through Meta B/Global B.
    _comprehend_and_ingest_fetched(episode)
    with phase_span("pressure", "neuromod", episode_id=ep_id):
        from autonomic.symphony_neuromod import (
            compute_episode_neuromod,
            set_current_memory_salience,
        )

        auto = getattr(episode, "auto", None)
        population = store.recent_outcome_population()
        baseline, baseline_source = _neuromod_expectation(population)
        auto_dict = auto if isinstance(auto, dict) else {}
        try:
            from guardian.pressure_observability import (
                log_episode_neuromod_compute_after,
                log_episode_neuromod_compute_before,
            )

            log_episode_neuromod_compute_before(
                episode_id=ep_id,
                baseline=baseline,
                auto=auto_dict,
            )
        except Exception:
            pass
        nm = compute_episode_neuromod(auto_dict, baseline)
        if isinstance(auto, dict):
            episode.auto["neuromod"] = nm
            # What the expectation rested on: the counts behind it, and whether it was measured
            # or the stated prior — so a coin-flip prior is never read back as a measurement.
            episode.auto["neuromod_expectation"] = {
                "value": baseline,
                "source": baseline_source,
                "population": population.to_dict(),
            }
            outputs = episode.auto.setdefault("outputs", {})
            receipt = outputs.get("association_use_receipt")
            if isinstance(receipt, dict):
                from memory.association_strength import finalize_verified_use_receipt

                outputs["association_outcome"] = finalize_verified_use_receipt(receipt, nm)
        set_current_memory_salience(nm)
        # Phase 1 (flag OMEGA_DOPAMINE_REINFORCE, default OFF): feed the dopamine RPE back to the
        # engrams the recall surfaced — strengthen on a rewarded surprise, weaken on a punished one.
        # Failure-soft: a reinforce hiccup must never break the neuromod stamp / cascade.
        try:
            from autonomic.neuromod_reinforce import reinforce_recalled_memories

            reinforce_recalled_memories(episode, nm)
        except Exception:
            pass
        try:
            from guardian.pressure_observability import log_episode_neuromod_compute_after

            log_episode_neuromod_compute_after(
                episode_id=ep_id,
                neuromod=nm,
                baseline=baseline,
            )
        except Exception:
            pass
    _start_meta_b_global_b(store, episode, cascade_status)


# Phase 6 self-drive: REMOVED (operator directive 2026-06-25). The deterministic
# `_stage_skill_continuation` staged an `execution_continuation` contributor from the
# foldback's continuation_pressure, auto-igniting the next cycle BEFORE the mind decided —
# so on a finished, verified build Omega chose IDLE ("task completed and verified, no
# pressure warranted") yet the loop re-fired the build anyway, deterministically overriding
# the mind into a self-perpetuating loop. Continuation is now the MIND's call alone: Omega
# episode cognition reasons over the result + the original input and raises pressure_params
# (→ ignites USE_SKILL next cycle) or idles. If there's nothing to act on, the loop stays
# quiet. The Global-B continuation_pressure is still RECORDED (documentary) but nothing
# deterministic acts on it. Intent routing (gap/continuation → USE_SKILL) is unchanged —
# that's how the mind's RAISED pressure becomes an action.


def _emit_build_complete_to_operator(
    episode,
    objective,
    qr,
    origin_opened_by: str = "",
    original_request: str = "",
    ported_to: str = "",
) -> None:
    """Proactively tell the operator their build is COMPLETE — this closes the fold-back loop.

    Fires only for an OPERATOR-requested build that actually finished (global_b objective_complete),
    so autonomous practice builds don't ping the operator. ``origin_opened_by`` is the build's
    ORIGIN carried by the SRAM execution report — a resumed build can finish after its original
    operator episode, so without the carried origin the notice never fired past cycle 1 (audit finding
    2026-07-01; deterministic layer owns completion→notify). The message rides the proactive-chat
    channel the HUD already polls. Delivery failure propagates so completion is never
    recorded while the operator-facing terminal action was silently lost.
    """
    try:
        opened_by = str(
            getattr(episode, "opened_by", None)
            or (dict(getattr(episode, "pressure", None) or {})).get("opened_by")
            or ""
        )
        effective = str(origin_opened_by or "").strip() or opened_by
        if not effective.startswith("operator"):
            return  # autonomous build -> no operator ping
        obj = str(objective or "").strip()
        qr = qr or {}
        msg = _compose_build_notice_text(
            objective=obj,
            original_request=original_request,
            summary=str(qr.get("summary") or "").strip(),
            files=qr.get("files_changed") or (),
        )
        if str(ported_to or "").strip():
            # where the finished work now LIVES — the operator's completed directory,
            # outside the agent's jail (workspace_port, operator 2026-07-03)
            msg += f"\n📁 {str(ported_to).strip()}"
        from pressure_loop.proactive_chat_messages import append_update

        append_update(
            text=msg,
            episode_id=_episode_id(episode),
            opened_by=effective,  # the build's ORIGIN, not the resume's execution_gap
            intent="PROACTIVE_RESPONSE",
        )
        _persist_notice_to_chat_history(msg)
        _mark_operator_notice_posted(episode)
    except Exception:
        log.exception("build-complete operator notice failed")
        raise


def _validate_skill_foldback_lineage(episode, pack: dict, result: dict) -> None:
    """Reject executor results that do not belong to this skill dispatch."""
    episode_id = str(getattr(episode, "episode_id", None) or "").strip()
    if not episode_id:
        raise ValueError("skill foldback result requires an active episode_id")
    result_episode_id = str(result.get("episode_id") or "").strip()
    if result_episode_id != episode_id:
        raise ValueError("skill foldback result episode_id mismatch")

    pack_episode_id = str(pack.get("episode_id") or "").strip()
    if pack_episode_id and pack_episode_id != episode_id:
        raise ValueError("skill foldback result execution pack episode_id mismatch")
    execution_inputs = dict(pack.get("execution_inputs") or {})
    skill_pack = dict(execution_inputs.get("skill_execution_pack") or {})
    skill_episode_id = str(skill_pack.get("episode_id") or "").strip()
    if skill_episode_id and skill_episode_id != episode_id:
        raise ValueError("skill foldback result skill pack episode_id mismatch")

    expected_dispatch = str(skill_pack.get("dispatch_id") or "").strip()
    if expected_dispatch and str(result.get("dispatch_id") or "") != expected_dispatch:
        raise ValueError("skill foldback result dispatch_id mismatch")
    selected = dict(skill_pack.get("selected_skill") or {})
    for key in ("skill_id", "skill_name"):
        expected = str(selected.get(key) or "").strip()
        if expected and str(result.get(key) or "") != expected:
            raise ValueError(f"skill foldback result {key} mismatch")


def _align_skill_outcome_with_verification(episode, verification) -> None:
    """Make deterministic workspace verification authoritative over foldback claims."""
    if not isinstance(verification, dict):
        return
    meta = dict(getattr(episode, "meta", None) or {})
    meta["execution_verification"] = dict(verification)
    episode.meta = meta

    global_ = dict(getattr(episode, "global_", None) or {})
    outcome = dict(global_.get("skill_outcome") or {})
    if not outcome:
        return
    outcome["verification"] = dict(verification)
    if not bool(verification.get("all_green")):
        blockers = [
            str(row) for row in (outcome.get("unresolved_blockers") or ()) if str(row).strip()
        ]
        for check in verification.get("checks") or ():
            if not isinstance(check, dict) or bool(check.get("ok", check.get("passed", False))):
                continue
            label = str(check.get("name") or check.get("check") or "verification_failed")
            blocker = f"verification_failed:{label}"
            if blocker not in blockers:
                blockers.append(blocker)
        signals = [
            str(row) for row in (outcome.get("continuation_signals") or ()) if str(row).strip()
        ]
        if "verification_needed" not in signals:
            signals.append("verification_needed")
        outcome.update(
            intent_satisfied=False,
            objective_complete=False,
            more_work_needed=True,
            unresolved_blockers=blockers,
            continuation_signals=signals,
            close_episode=False,
        )
    global_["skill_outcome"] = outcome
    episode.global_ = global_


def _hold_execution_report_from_fold(
    episode,
    qwen_result: dict[str, Any],
    folded: dict[str, Any],
    *,
    build_task: str,
    pass_gate: tuple[str, ...],
    verification: dict[str, Any] | None,
    original_request: str,
    origin_opened_by: str,
) -> None:
    """Put the builder's complete cold-resume boundary object in episode SRAM."""
    from memory.build_objective import current_build_objective
    from memory.execution_report import ExecutionReport, hold_execution_report

    def _unique(rows) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(row).strip() for row in (rows or ()) if str(row).strip()))

    files = _unique(qwen_result.get("files_changed"))
    commands = _unique(qwen_result.get("commands_run"))
    did = tuple(f"changed {path}" for path in files) + tuple(
        f"ran {command}" for command in commands
    )
    verdict = dict(verification or {})
    succeeded: list[str] = []
    failed: list[str] = []
    for check in verdict.get("checks") or ():
        if not isinstance(check, dict):
            continue
        name = str(check.get("name") or "verification").strip()
        detail = str(check.get("detail") or "").strip()
        statement = f"{name}: {detail}" if detail else name
        (succeeded if check.get("ok") else failed).append(statement)
    global_b = dict(folded.get("global_b") or {})
    blockers = _unique(
        tuple(qwen_result.get("unresolved_blockers") or ())
        + tuple(global_b.get("unresolved_blockers") or ())
    )
    failure_class = str(qwen_result.get("failure_class") or "").strip()
    if failure_class:
        failed.append(failure_class)
    failed.extend(blockers)
    build_objective = current_build_objective()
    practice_domain = build_objective.practice_domain if build_objective is not None else ""
    artifact_revisions, verification_receipts = _episode_evidence_receipts(_episode_id(episode))
    job_id = str(qwen_result.get("job_id") or "").strip()
    run_provenance = None
    if job_id:
        from memory.run_anchor import run_provenance_for_job

        run_provenance = run_provenance_for_job(job_id)
    hold_execution_report(
        ExecutionReport(
            episode_id=_episode_id(episode),
            build_task=build_task,
            pass_gate=pass_gate,
            did=did,
            did_not=blockers,
            succeeded=_unique(succeeded),
            failed=_unique(failed),
            summary=str(qwen_result.get("summary") or ""),
            progress_digest=str(qwen_result.get("progress_digest") or ""),
            files_changed=files,
            verification=verdict,
            original_request=original_request,
            origin_opened_by=origin_opened_by,
            rounds_used=int(qwen_result.get("rounds_used") or 0),
            recent_messages=tuple(qwen_result.get("recent_messages") or ()),
            metadata={
                "status": str(qwen_result.get("status") or ""),
                "objective_complete": bool(global_b.get("objective_complete")),
                "more_work_needed": bool(global_b.get("more_work_needed")),
                "practice_domain": practice_domain,
            },
            artifact_revisions=artifact_revisions,
            verification_receipts=verification_receipts,
            run_provenance=run_provenance,
        )
    )


def _episode_evidence_receipts(episode_id: str) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """This episode's recorded artifact-revision and verification receipts, in capture order."""
    from memory.auto_evidence import ARTIFACT_REVISION, VERIFICATION_RESULT
    from memory.episode_events import read_episode_events
    from schema.continuity_json import unfreeze

    artifacts: list[dict] = []
    verifications: list[dict] = []
    for event in read_episode_events(episode_id):
        payload = dict(unfreeze(event.payload))
        payload.pop("episode_id", None)
        if event.event_kind == ARTIFACT_REVISION:
            artifacts.append(payload)
        elif event.event_kind == VERIFICATION_RESULT:
            verifications.append(payload)
    return tuple(artifacts), tuple(verifications)


def _run_skill_foldback(episode) -> None:
    """Phase 6 (flag-gated): for a USE_SKILL episode, fold the qwen skill result through the
    skill-specific Meta B/Global B (6K) and Omega/DeepSleep capability consolidation (6L),
    ALONGSIDE the live reflection/outcome (which already classified the auto outcome). Inert for
    non-USE_SKILL episodes; a failed fold is a failed episode boundary."""
    meta = dict(getattr(episode, "meta", None) or {})
    pack = dict(meta.get("execution_action_pack") or {})
    from auto_loop.motor_adapter import effective_motor_action

    try:
        fold_action = effective_motor_action(pack)
    except (TypeError, ValueError):
        return
    if fold_action != "USE_SKILL":
        return
    auto = dict(getattr(episode, "auto", None) or {})
    outputs = dict(auto.get("outputs") or {})
    qr = dict(outputs.get("skill_result") or {})
    if not qr:
        return  # no skill result (e.g. missing_skill_pack) — nothing to fold
    _validate_skill_foldback_lineage(episode, pack, qr)
    objective = str(
        outputs.get("skill_objective")
        or dict(pack.get("execution_inputs") or {}).get("objective")
        or ""
    )
    _sp = dict(pack.get("execution_inputs") or {}).get("skill_execution_pack") or {}
    evidence_required = tuple(_sp.get("evidence_required") or ())
    raw_program = meta.get("motor_program")
    acceptance_criteria = (
        tuple(dict(raw_program.get("outcome_contract") or {}).get("acceptance_criteria") or ())
        if isinstance(raw_program, dict)
        else ()
    )
    if not acceptance_criteria:
        acceptance_criteria = tuple(_sp.get("completion_criteria") or evidence_required)
    if isinstance(raw_program, dict) and raw_program.get("shadow") is not True:
        # The transport action is intentionally rewritten to USE_SKILL before
        # execution.  Detect motor authority from the immutable attached program,
        # not from that mutable transport label, or a selected skill's unrelated
        # evidence requirements can replace Omega's contract during fold-back.
        from schema.qwen_skill_dispatch import motor_authority

        objective, evidence_required, acceptance_criteria, _ = motor_authority(raw_program)
        try:
            from auto_loop.motor_adapter import execution_evidence_from_fold

            factual_evidence = execution_evidence_from_fold(episode).to_dict()
            for lane in ("artifacts", "sources_consulted", "verification_results"):
                qr[lane] = factual_evidence.get(lane) or []
        except (TypeError, ValueError):
            log.exception("motor foldback factual evidence projection failed")
    _project_outcome = "failed"
    _project_verification = None
    try:
        from meta_loop.skill.foldback import (
            MetaBSkillLedger,
            attach_skill_foldback,
            fold_skill_result,
        )

        ep = _episode_id(episode)
        with phase_span("meta", "skill_foldback", episode_id=ep):
            ledger = MetaBSkillLedger()
            folded = fold_skill_result(
                qr,
                objective=objective,
                evidence_required=evidence_required,
                ledger=ledger,
            )
            attach_skill_foldback(episode, folded)
            # VERDICT EMIT (operator 2026-07-02): this verdict is what decides notice-vs-stash
            # below, and every path out of it is silent by design (a blocked success is not an
            # error) — live episode 384e9e9c hid an evidence_unsatisfied blocker here and the
            # operator's completion notice never fired with nothing in the log to say why. One
            # DATA line makes the branch decision auditable. Failure-soft.
            try:
                from guardian.terminal_feedback import emit_data as _emit_verdict

                _mb_v = dict(folded.get("meta_b") or {})
                _gb_v = dict(folded.get("global_b") or {})
                _blockers = ",".join(str(b) for b in (_gb_v.get("unresolved_blockers") or ()))
                _emit_verdict(
                    "meta",
                    "skill_foldback_verdict",
                    status="ok",
                    ok=True,
                    episode_id=ep,
                    outcome_kind=str(_mb_v.get("outcome_kind") or ""),
                    objective_complete=bool(_gb_v.get("objective_complete")),
                    more_work_needed=bool(_gb_v.get("more_work_needed")),
                    blockers=_blockers,
                    summary=(
                        f"{_mb_v.get('outcome_kind')} -> objective_complete="
                        f"{bool(_gb_v.get('objective_complete'))} more_work="
                        f"{bool(_gb_v.get('more_work_needed'))}"
                        + (f" blockers={_blockers}" if _blockers else "")
                    ),
                )
            except Exception:
                pass
            _ep_opened = _episode_origin_opened_by(episode)
            from memory.execution_report import current_execution_report

            prior_report = current_execution_report()
            _orig_request = _operator_request_text(episode)
            if not _orig_request and prior_report is not None:
                _orig_request = prior_report.original_request
            if not _ep_opened and prior_report is not None:
                _ep_opened = prior_report.origin_opened_by
            _gb = dict(folded.get("global_b") or {})
            _gate_verif = None
            if qr.get("files_changed"):
                from core.build_verification import verify_build_against_request

                _gate_verif = verify_build_against_request(
                    original_request=_orig_request or str(objective or ""),
                    files=qr.get("files_changed") or (),
                )
            _align_skill_outcome_with_verification(episode, _gate_verif)
            _complete = bool(_gb.get("objective_complete")) and (
                _gate_verif is None or bool(_gate_verif.get("all_green"))
            )
            _project_outcome = "completed" if _complete else "in_progress"
            _project_verification = _gate_verif

            _hold_execution_report_from_fold(
                episode,
                qr,
                folded,
                build_task=objective,
                pass_gate=tuple(acceptance_criteria),
                verification=_project_verification,
                original_request=_orig_request,
                origin_opened_by=_ep_opened,
            )

            from deepsleep_loop.skill_consolidation import consolidate_skill_episode

            consolidated = consolidate_skill_episode(folded, qr, objective=objective)
            meta2 = dict(getattr(episode, "meta", None) or {})
            meta2["skill_consolidation"] = consolidated
            # Snapshot the normalized skill result into META so the taxonomy capture (which runs
            # AFTER store.close clears the live auto.outputs) can still emit the qwen_skill_result
            # event — the SOLE source workflow_topology builds the recallable workflow_memory node
            # from. Without this, a finished build left NO artifact memory (operator finding
            # 2026-06-25): Omega couldn't recall what it had built.
            meta2["skill_result_snapshot"] = qr
            meta2.pop("skill_foldback_error", None)
            episode.meta = meta2
            # Continuation is the MIND's call now (deterministic self-drive removed above):
            # Omega episode cognition (runs next, line ~988) reasons over this result + the
            # original input and either raises pressure_params to continue or idles to stop.
        from guardian.terminal_feedback import emit_data

        try:
            emit_data(
                "pressure",
                "skill_foldback",
                status="ok",
                ok=True,
                episode_id=ep,
                skill_status=str(qr.get("status") or ""),
            )
        except Exception:
            pass
    except Exception as exc:
        meta2 = dict(getattr(episode, "meta", None) or {})
        meta2["skill_foldback_error"] = str(exc)
        episode.meta = meta2
        from guardian.terminal_feedback import emit_data

        try:
            emit_data(
                "pressure",
                "skill_foldback",
                status="fail",
                ok=False,
                error=str(exc),
                episode_id=_episode_id(episode),
            )
        except Exception:
            pass
        raise


def _execute_pending_workspace_port(ep_id: str) -> None:
    """Execute THIS episode's recorded workspace port (if any) — the finished build moves
    to the operator's completed directory and the workspace is cleared for the next build.

    Runs ONCE at the cascade tail: provably after the dev-lobe persist, rollback capture,
    consolidation and every completion site — all of which read workspace files from disk.
    take_port is episode-scoped, so a stale record from a crashed episode is dropped, never
    executed against a newer build's in-flight files. Failure-soft."""
    try:
        from core.workspace_port import execute_port, take_port

        record = take_port(ep_id)
        if record is not None:
            execute_port(record)
    except Exception:
        log.exception("workspace port failed (non-fatal)")


def _start_meta_b_global_b(store, episode, cascade_status: str = "completed") -> None:
    """Meta B + Global B: AutoOutcomePack → MetaReflectionPack → GlobalOutcomePack."""
    ep_id = _episode_id(episode)
    from memory.cognition_capture import (
        GLOBAL_B_FAILED,
        META_B_FAILED,
        OMEGA_FAILED,
        capture_phase_failure,
        new_invocation_id,
    )

    def _run_recorded_phase(phase, event_kind, producer):
        invocation = new_invocation_id()
        try:
            return producer()
        except Exception as exc:
            reason = f"{phase} raised {type(exc).__name__}: {exc}"
            _record_phase_failure(
                lambda: capture_phase_failure(
                    episode,
                    phase=phase,
                    event_kind=event_kind,
                    reason=reason,
                    invocation_id=invocation,
                ),
                producer_error=exc,
            )
            raise
    with phase_span("pressure", "meta_b_global_b_handoff", episode_id=ep_id):
        with phase_span("pressure", "meta_b_handoff", episode_id=ep_id):
            from meta_loop.meta_b import run_meta_b_front

            def _run_meta_b():
                if run_meta_b_front(episode) is None:
                    raise RuntimeError("Meta B produced no reflection — invalid cognitive state")

            _run_recorded_phase("meta_b", META_B_FAILED, _run_meta_b)
        with phase_span("pressure", "global_b_handoff", episode_id=ep_id):
            from global_loop.loop import run_global_b

            meta = dict(getattr(episode, "meta", None) or {})
            reflection_dict = dict(meta.get("reflection") or {})

            _run_recorded_phase(
                "global_b",
                GLOBAL_B_FAILED,
                lambda: run_global_b(episode, reflection_dict),
            )
    # Snapshot the initial request before fold-back updates the SRAM report.
    # This is the immutable comparison target for Omega's post-agent satisfaction decision.
    from omega_loop.episode.packet import capture_execution_request_context

    execution_request = capture_execution_request_context(
        episode,
        request_text=_operator_request_text(episode),
    )
    _run_skill_foldback(episode)
    # The fold just recorded this yield's report — patch it into the already-attached packet so
    # the monitor prompt renders the same snapshot the gate reads (one truth, no cycle skew).
    _refresh_packet_wip_from_execution_report(episode)
    # Global B attached its packet before skill foldback existed. Refresh exactly
    # once now so Omega sees the final, lineage-checked executor evidence/verdict.
    from omega_loop.episode.packet import refresh_omega_execution_foldback

    # The carried origin wins for a durable job continuation.
    # request_origin lands in the ExecutionFoldback contract Omega judges, and
    # capture_execution_request_context resolves it from the episode's own
    # provenance whenever no SRAM report exists yet — which is exactly the state
    # of a continuation's FIRST fold. Without this the mind is told the
    # operator's build was requested by "agent_motor_dispatch". Empty for every
    # other episode, so their origin resolution is unchanged.
    from pressure_loop.agent_lane import carried_origin_opened_by

    refresh_omega_execution_foldback(
        episode,
        request_text=execution_request["text"],
        request_origin=carried_origin_opened_by(episode) or execution_request["origin"],
    )
    auto = dict(getattr(episode, "auto", None) or {})
    prompt_type = str(dict(auto.get("prompt_pack") or {}).get("prompt_type") or "")
    global_ = dict(getattr(episode, "global_", None) or {})
    omega_intent = str(dict(global_.get("omega_packet") or {}).get("intent") or "")
    fold_decision = True
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "omega_fold_decision",
            episode_id=ep_id,
            summary=f"fold_through_omega={fold_decision}",
            fold_decision=fold_decision,
            prompt_type=prompt_type,
            omega_intent=omega_intent,
        )
    except Exception:
        pass
    pace_autonomous_phase(episode, "omega")
    omega_invocation = new_invocation_id()
    try:
        omega_result = _run_omega_episode_cognition(episode)
    except Exception as exc:
        reason = f"Omega raised {type(exc).__name__}: {exc}"
        _record_phase_failure(
            lambda: capture_phase_failure(
                episode,
                phase="omega",
                event_kind=OMEGA_FAILED,
                reason=reason,
                invocation_id=omega_invocation,
            ),
            producer_error=exc,
        )
        raise
    if omega_result is None:
        # T6: no valid control means Omega produced no decision over the mail this episode
        # bound, so hand it back NOW. This is correctness, not latency: a contained fold
        # failure still reaches the terminus, archives, and the after_archive seam would
        # otherwise ACKNOWLEDGE operator instructions that no fold ever reasoned over. A
        # fold that RAISES never reaches the terminus, so its leases simply expire.
        from omega_loop.episode.packet import release_omega_mail

        release_omega_mail(episode)
    from schema.omega_control_decision import OmegaControlDecision

    _settle_operator_turn(episode, None if omega_result is None
                          # From the carrier, not the result object: a replay settles the
                          # fold's ORIGINAL control, frozen with its statements (T8).
                          else OmegaControlDecision.from_dict(
                              dict(dict(getattr(episode, "global_", None) or {})
                                   .get("omega_result") or {}).get("control_decision")
                              or omega_result.control_decision))
    from pressure_loop import window_lane

    if window_lane.is_window_episode(episode):
        _commit_window_reflection(episode, omega_result)
    # WORKSPACE PORT (operator 2026-07-03): a build ending recorded by any completion/stop
    # site above executes HERE — after every workspace disk reader in the cascade.
    _execute_pending_workspace_port(ep_id)
