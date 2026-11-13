"""Pure sequencer for AUTO phase chain."""

from __future__ import annotations

from copy import deepcopy
import time
from typing import Any, Callable, Dict, Optional

from guardian.terminal_feedback import phase_span

from auto_loop import execute as execute_phase
from auto_loop import fold as fold_phase
from auto_loop import intake as intake_phase
from auto_loop import promptpack as promptpack_phase
from auto_loop import recall as recall_phase
from auto_loop import route as route_phase
from auto_loop import validate as validate_phase
from auto_loop._auto_state import AutoOutcomePack, MemoryEnvelope
from auto_loop.mril_tools import memory_recall_required
from auto_loop.motor_adapter import effective_motor_action
from pressure_loop.operator_chat_responses import waiting_chat_turn


def _ep_id(episode_or_ai) -> str:
    return str(getattr(episode_or_ai, "episode_id", "") or "no-episode")


def _auto_cycle_status(failure_kind: Optional[str], action: str) -> tuple[str, str]:
    """Map an executor outcome to ``(status, cycle_kind)``.

    ``apply_pending_approval`` is a STAGED-GATE HOLD (Phase 5x), not a failure:
    the APPLY was skipped awaiting an operator approval token. It is classified
    ``skipped`` so it stays NEUTRAL downstream — neither a success (no mastery
    grounding, no dopamine) nor a failure (no neuromod stress spike, no
    failure-analysis, not counted against success-rate). During the staged
    window this is the common, expected state and must not poison telemetry.
    """
    if failure_kind == "pending_input":
        return "skipped", "pending_input"
    if failure_kind in ("apply_pending_approval", "skill_requires_confirmation"):
        # a staged HOLD (APPLY) or a skill awaiting operator confirmation — neutral, skipped.
        return "skipped", "skipped"
    if failure_kind in ("skill_yielded", "skill_partial"):
        # A YIELD / PARTIAL is progress OWED, not a failure: the executor stopped at a clean
        # checkpoint (turn / watchdog budget) with real work done and MORE owed. Classify it as
        # PARTIAL -> the 'degraded' disposition downstream: non-failure, neuromod-neutral (no
        # stress spike, no dopamine ding), and NO failure-analysis. The mind then reads
        # 'partially completed' alongside the build_in_progress CONTINUE|FINISH prompt and DECIDES
        # whether to resume — instead of reading 'failed' and pivoting to diagnose a phantom
        # failure (operator live finding 2026-07-01). The continue/stop decision stays the mind's.
        return "partial", "partial"
    if failure_kind:
        return "failed", "fail"
    if action == "IDLE":
        return "idle", "idle"
    return "success", "success"


def _auto_domain(execution_pack: Dict[str, Any]) -> str:
    inputs = dict(execution_pack.get("execution_inputs") or {})
    dom = inputs.get("domain") or inputs.get("subdomain") or execution_pack.get("domain")
    return str(dom) if dom else ""


def _route_span_fields(execution_pack: Dict[str, Any]) -> dict[str, object]:
    fields: dict[str, object] = {}
    tool = str(execution_pack.get("tool") or "")
    if tool:
        fields["tool"] = tool
    domain = _auto_domain(execution_pack)
    if domain:
        fields["domain"] = domain
    return fields


def _execute_span_fields(
    execution_pack: Dict[str, Any],
    exec_result: Dict[str, Any],
) -> dict[str, object]:
    fields = _route_span_fields(execution_pack)
    tokens = exec_result.get("tokens")
    latency_s = exec_result.get("inference_latency_s")
    if tokens and latency_s and float(latency_s) > 0:
        fields["tokens"] = int(tokens)
        fields["tok_s"] = round(float(tokens) / float(latency_s), 1)
    return fields


def _window_reflection_receipt(
    episode, execution_pack: Dict[str, Any], memory_envelope: MemoryEnvelope
) -> Optional[Dict[str, Any]]:
    """Structural AUTO for a window episode, or None for every other action.

    A window episode's action executes nothing and infers nothing: AUTO attests that
    the locked window Meta A named is the one admission carried onto this episode.
    The reflection itself is Omega's window-lane fold after Global B.
    """
    if execution_pack.get("action") != "WINDOW_REFLECTION":
        return None
    from pressure_loop import window_lane

    window = window_lane.carried_window(episode)
    window_id = dict(execution_pack.get("execution_inputs") or {}).get("window_id")
    if window is None or window.window_id != window_id:
        raise RuntimeError("window reflection pack does not name the window this episode carries")
    return {
        "outputs": {
            "window_reflection": {
                "window_id": window.window_id,
                "episode_count": int(window.episode_count),
                "status": "attached",
            }
        },
        "failure_kind": None,
        "failure_reason": None,
        "model_id": None,
        "warnings": [],
        "tokens": None,
        "inference_latency_s": None,
        "memory_envelope": memory_envelope,
    }


def _is_window_pack(episode) -> bool:
    pack = dict(getattr(episode, "meta", None) or {}).get("execution_action_pack") or {}
    return dict(pack).get("action") == "WINDOW_REFLECTION"


def _execute_with_lane_entry(lane_entry):
    """``execute`` bracketed by the continuation's lane entry (T9).

    The lane's first inference delivers the mail (``execute``'s ``_generate``); here the
    run is closed — performed, or never shown — and a refusal raised inside the lane
    becomes the loud bounded failure it is, whatever the lane made of the exception.
    """
    if lane_entry is None:
        return execute_phase.execute

    from auto_loop.lane_entry import LaneEntryRefused

    def run(prompt_pack, model_profile, execution_pack, memory_envelope, *args, **kwargs):
        result = None
        try:
            # From here only the lane's own declarations can say nothing ran; cleanup cannot.
            lane_entry.enter()
            result = execute_phase.execute(prompt_pack, model_profile, execution_pack,
                                           memory_envelope, *args, lane_entry=lane_entry, **kwargs)
        except LaneEntryRefused:
            # An EXPECTED refusal is AUTO's bounded failure, never an exception out of the
            # cascade: Meta B, Global B and Omega still reflect on it (review 2026-09-17).
            pass
        finally:
            # Required mail the lane neither showed nor declared unshowable: read before close.
            unshown = lane_entry.state == "claimed" and lane_entry.required
            lane_entry.close(ran=True)
        refusal = lane_entry.refusal
        if refusal is None and not unshown:
            return result
        kind = refusal.kind if refusal is not None else "lane_entry_not_consumed"
        reason = (refusal.reason if refusal is not None else
                  "the lane returned without showing its required lane-entry mail")
        failed = dict(result or {})
        # Nothing the lane produced past a refusal is an outcome.
        failed.update(outputs={}, failure_kind=kind, failure_reason=reason)
        for key, default in (("model_id", None), ("warnings", []), ("tokens", None),
                             ("inference_latency_s", None), ("memory_envelope", memory_envelope)):
            failed.setdefault(key, default)
        return failed

    return run


def run_auto(
    episode,
    *,
    runtime_client: Any = None,
    tool_invoker: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    memory: Optional[Callable[..., Any]] = None,
) -> Optional[AutoOutcomePack]:
    """Pixels belong to this attempt only, including failed validation/inference."""
    try:
        return _run_auto(episode, runtime_client=runtime_client,
                         tool_invoker=tool_invoker, memory=memory)
    finally:
        if hasattr(episode, "_browser_perception_attachment"):
            del episode._browser_perception_attachment
        entry = getattr(episode, "_lane_entry", None)
        if entry is not None and not _is_window_pack(episode):
            # Whatever happened on the way to the lane (a recall or prompt-pack failure): a
            # claim the lane never consumed goes back NOW. A no-op once the lane closed it.
            # A window episode keeps its entry: its first inference is Omega's window fold.
            entry.close(ran=False)


def _run_auto(
    episode,
    *,
    runtime_client: Any = None,
    tool_invoker: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    memory: Optional[Callable[..., Any]] = None,
) -> Optional[AutoOutcomePack]:
    """AUTO movement: validate → recall → prompt → route → execute → fold → emit."""
    with phase_span("auto", "intake", episode_id=_ep_id(episode)):
        ai = intake_phase.auto_input_from_episode(episode)
    if ai is None:
        return None

    ep_id = _ep_id(ai)
    t0 = time.perf_counter()

    # LANE ENTRY (integration spec section 4, T9): the continuation's mail is claimed, and
    # its prerequisites enforced, before anything else runs. A refusal is a loud bounded
    # failure — the dependent action does not execute, and the job is left as it stands.
    from auto_loop.lane_entry import LaneEntryRefused, open_lane_entry

    lane_entry = None
    try:
        lane_entry = open_lane_entry(episode, ai.execution_pack)
        ok, failure_kind, failure_reason = True, None, None
    except LaneEntryRefused as refusal:
        ok, failure_kind, failure_reason = False, refusal.kind, refusal.reason
    # Process-local, like the perception attachment: a window episode's first inference is
    # Omega's window fold, which delivers from here.
    episode._lane_entry = lane_entry

    if ok:
        with phase_span("auto", "validate", episode_id=ep_id):
            ok, failure_kind, failure_reason = validate_phase.validate_for_execution(ai.execution_pack)
    if not ok:
        outcome = execute_phase._build_failure_outcome(
            ai,
            failure_kind=str(failure_kind),
            failure_reason=str(failure_reason),
        )
        # Keep the same folded episode shape as the normal execute path. Writing only
        # outcome.auto here drops the independent Auto-side execution pack and makes
        # validation failures observe a different contract than runtime failures.
        episode.auto = {
            "episode_id": ai.episode_id,
            "source_action_goal": deepcopy(ai.source_action_goal),
            "action": ai.execution_pack.get("action"),
            "execution_pack": dict(ai.execution_pack),
            "status": outcome.auto.get("status"),
            "failure_kind": outcome.auto.get("failure_kind"),
            "failure_reason": outcome.auto.get("failure_reason"),
            "memory": {
                "memory_required": outcome.auto.get("memory_status") != "skipped",
                "memory_status": outcome.auto.get("memory_status"),
                "memory_reason": outcome.auto.get("memory_reason"),
                "memory_scope": list(outcome.auto.get("memory_scope") or ()),
                "items": [],
                "error": None,
            },
            "model_profile": None,
            "model_id": None,
            "prompt_pack": {},
            "outputs": {},
            "telemetry": {"cycle_kind": "fail", "runtime_ms": 0, "warnings": []},
        }
        return outcome

    with phase_span("auto", "recall", episode_id=ep_id):
        memory_envelope = recall_phase.memory_envelope_before_execute(ai.execution_pack)

    with phase_span("auto", "promptpack", episode_id=ep_id):
        prompt_pack = promptpack_phase.build_prompt_pack(
            ai.execution_pack,
            memory_envelope,
            pressure=ai.pressure,
            temporal=ai.temporal,
        )
    memory_enriched = bool(memory_recall_required(ai.execution_pack))
    with phase_span("auto", "route", episode_id=ep_id) as span:
        model_profile = route_phase.select_model_profile(
            prompt_pack.prompt_type,
            memory_enriched=memory_enriched,
            tool_call_is_direct=route_phase.tool_call_is_direct(ai.execution_pack),
        )
        route_fields = _route_span_fields(dict(ai.execution_pack))
        if route_fields:
            span.set(**route_fields)

    with phase_span("auto", "execute", episode_id=ep_id) as span:
        from runtime.socket import pipeline_context_for_episode

        request_context = pipeline_context_for_episode(episode)
        browser_inputs = ({"browser_perception_attachment":
                           getattr(episode, "_browser_perception_attachment", None)}
                          if ai.execution_pack.get("action") == "BROWSER_ACTION" else {})
        exec_result = _window_reflection_receipt(episode, ai.execution_pack, memory_envelope)
        if exec_result is None:
            exec_result = _execute_with_lane_entry(lane_entry)(
                prompt_pack,
                model_profile,
                ai.execution_pack,
                memory_envelope,
                ai.episode_id,
                **browser_inputs,
                runtime_client=runtime_client,
                tool_invoker=tool_invoker,
                memory=memory,
                temporal=ai.temporal,
                opened_by=str((ai.pressure or {}).get("reason") or ""),
                # RISK 5: the operator turn whose bubble is waiting on this reply, so
                # the response stage can stream its tokens there. Derived by the same
                # function cognition_chain uses to deliver the FINISHED reply — the
                # stream and the answer must address one bubble, not two.
                chat_cycle_id=str(
                    (waiting_chat_turn(ai.pressure) or {}).get("cycle_id") or ""
                ),
                pipeline_id=request_context.get("pipeline_id"),
                work_kind=str(request_context.get("work_kind") or "autonomous"),
                job_id=str(request_context.get("job_id") or ""),
                # T3: the carrier's own thread. A question this batch asks records the
                # thread that asked it, which neither the prompt pack nor the ingress
                # contributor can supply for a freshly rooted episode.
                thread_id=str(getattr(episode, "thread_id", "") or ""),
                request_cycle_id=str(request_context.get("cycle_id") or ep_id),
                arrival_seq=next((int((row.get("detail") or row.get("metadata") or {}).get("arrival_seq") or 0)
                                  for row in (ai.pressure or {}).get("contributors", ())
                                  if (row.get("detail") or row.get("metadata") or {}).get("cycle_id") == request_context.get("cycle_id")), 0),
            )
        action = effective_motor_action(ai.execution_pack)
        if action != "IDLE":
            exec_fields = _execute_span_fields(dict(ai.execution_pack), exec_result)
            if exec_fields:
                span.set(**exec_fields)

    if memory_recall_required(ai.execution_pack):
        mril_mem = exec_result.get("memory_envelope")
        if isinstance(mril_mem, MemoryEnvelope):
            memory_envelope = mril_mem

    action = effective_motor_action(ai.execution_pack)
    status, cycle_kind = _auto_cycle_status(exec_result.get("failure_kind"), action)

    with phase_span("auto", "fold", episode_id=ep_id):
        folded = fold_phase._fold_episode_auto(
            ai,
            status=status,
            cycle_kind=cycle_kind,
            memory_envelope=memory_envelope,
            model_profile=model_profile,
            model_id=exec_result.get("model_id"),
            prompt_pack=prompt_pack,
            exec_result=exec_result,
            runtime_ms=int((time.perf_counter() - t0) * 1000),
        )
    episode.auto = folded
    return fold_phase.auto_outcome_from_fold(ai, folded)


run_auto_front = run_auto
