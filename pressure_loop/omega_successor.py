"""Validated semantic control becomes a new pressure admission, never AUTO work."""
from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace

from pressure_loop.contributors import make_contributor
from schema.browser_perception import bounded_text
from schema.omega_control_decision import OmegaControlDecision, omega_lane_for_intent

log = logging.getLogger(__name__)

_RUNNABLE_WORKFLOW = frozenset({"active", "waiting_for_operator"})
# Episodes that open on the operator's own turn; only their cycle is operator lineage.
_OPERATOR_OPENERS = frozenset({
    "operator_query", "operator_command", "operator_response", "plugin_permission_decision",
})
_AGENT_OBSERVATION_LIMIT = 12


def _agent_observations(episode, origin: dict) -> list[dict]:
    """A bounded digest of an operator agent task's tool results, carried to its next batch."""
    rows = [dict(row) for row in origin.get("agent_observations") or () if isinstance(row, dict)]
    outputs = dict(dict(getattr(episode, "auto", None) or {}).get("outputs") or {})
    for record in outputs.get("operator_tool_calls") or ():
        if isinstance(record, dict):
            rows.append({
                "tool": str(record.get("tool_name") or "")[:80],
                "args": json.dumps(record.get("args") or {}, ensure_ascii=False, default=str)[:300],
                "status": str(record.get("status") or "")[:20],
                "output": str(record.get("output") or "")[:1500],
                "error": str(record.get("error") or "")[:300],
            })
    return rows[-_AGENT_OBSERVATION_LIMIT:]


def validate_successor(metadata: dict) -> OmegaControlDecision:
    from pressure_loop.continuation import validate

    validate(metadata)
    control = OmegaControlDecision.from_dict(metadata["omega_control"])
    p = control.pressure
    if p is None:
        raise ValueError("DONE cannot be a successor")
    parent = bounded_text(metadata.get("parent_episode_id"), "parent_episode_id", 128)
    identity = f"omega-successor-{uuid.uuid5(uuid.NAMESPACE_URL, parent).hex}"
    expected = dict(successor_id=identity, cycle_id=identity, source_episode_id=parent,
                    directive_intent=p.intent, reason=p.reason, desired_state=p.desired_state,
                    observed_state=p.observed_state, resolution_conditions=list(p.resolution_conditions),
                    omega_lane=omega_lane_for_intent(p.intent))
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError("successor metadata differs from validated control or lineage")
    if metadata.get("lane_work_handoff") is True and metadata.get("lane") != expected["omega_lane"]:
        raise ValueError("specialist successor lane conflicts with its validated intent")
    bounded_text(metadata.get("original_objective"), "original_objective", 16000)
    if p.intent == "BROWSE":
        bounded_text(metadata.get("browser_workflow_id"), "browser_workflow_id", 128)
        if type(metadata.get("browser_workflow_revision")) is not int:
            raise ValueError("browser successor names the workflow revision it continues")
    return control


def _decline(parent: str, reason: str) -> None:
    """A successor that could never be admitted is refused loudly, never staged or raised."""
    log.warning("omega successor for episode %s declined: %s", parent, reason)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("pressure", "omega_successor_declined", episode_id=parent,
                  summary=reason[:200], status="degraded", ok=False)
    except Exception:
        pass


def cancellation_state(metadata: dict) -> tuple[str, str]:
    """Why the operator's stop forbids this continuation, or None when nothing stopped it.

    An unambiguous stop is a host-owned halt (spec section 11, T12): ``halt_operator_work``
    raises the turn's WORK halt and requests the job's stop. Both outlive the batch that was
    running, which is what makes them checkable here — a halt or cancel that lands after a
    successor was staged would otherwise be resurrected by the next admission, restarting
    work the operator stopped. Checked TWICE: at publication (``build_omega_successor``,
    which declines) and again at admission (the pressure gather, through
    ``unrunnable_successor_reason``, which retires the staged row loudly).

    A pending stop revision counts, and so does an acknowledged one: the operator's stop is
    authority the moment it lands, and the batch acknowledges it at its next boundary. The
    job's ``stopped`` STATUS is NOT that authority, because the host stops jobs too — every
    boot stops the ones whose writer died with the process, which is the state a successor
    exists to continue. Read failure-soft — a job record that cannot be read stops nothing by
    itself.

    Both durable records are consulted before the process-local work-halt flag, which is an
    execution signal with a lease and is NOT the authority: a jobless continuation outlived
    that flag's TTL and was admitted (review 2026-09-18). ``pressure.work_stopped`` is the
    durable authority; the flag is kept only as a same-process corroboration for a halt
    raised by a path that did not reach the ledger.
    """
    from auto_loop.execute import job_control as jobs
    from pressure_loop.operator_chat_responses import work_halt_requested
    from pressure_loop.pressure import stop_standing

    unknown = ""
    job_id = str(metadata.get("agent_job_id") or "")
    if job_id:
        try:
            job = jobs.get_job(job_id)
        except KeyError:
            job = None                 # no such job: gone, not stopped
        except Exception as exc:  # noqa: BLE001 - a record that cannot be READ decides nothing
            log.warning("could not read job %s while judging cancellation", job_id, exc_info=True)
            job, unknown = None, f"job {job_id} cannot be read: {type(exc).__name__}: {exc}"
        if job is not None:
            # The OPERATOR'S stop, and only that. Both halt paths leave a durable witness on
            # the record — ``halt_operator_work`` requests the stop (a ``stop`` revision) and
            # the batch acknowledges it at its boundary (a stop receipt) — and neither is ever
            # pruned, so this reads the authority rather than its side effect.
            if any(row.kind == "stop" for row in job.revisions):
                return "stopped", f"job {job_id} has the operator's stop pending"
            if job.stop_receipts:
                return "stopped", f"job {job_id} acknowledged the operator's stop"
            # A ``stopped`` STATUS is not that authority (review 2026-09-19). An agent task's
            # job stays open between its batches, so every restart finds one running and
            # ``reconcile_jobs_at_boot`` stops them all — whose own contract is that
            # "unfinished work continues through Omega successors", and ``open_job`` reopens a
            # non-running job for exactly that successor. Reading the status here retired the
            # continuation that was supposed to do the reopening: one restart silently
            # cancelled work nobody stopped.
    lineage = str(metadata.get("original_cycle_id") or "")
    if lineage:
        standing, why = stop_standing(lineage)
        if standing == "stopped":
            return "stopped", why
        if standing == "unknown":
            unknown = unknown or why
        elif work_halt_requested(lineage):
            return "stopped", f"the operator halted the work of turn {lineage}"
    return ("unknown", unknown) if unknown else ("", "")


def cancellation_reason(metadata: dict) -> str | None:
    """The reason a CONFIRMED stop forbids this continuation, or None.

    Unknown state is not a reason: see ``cancellation_state``, which callers that must tell
    the two apart use instead.
    """
    standing, why = cancellation_state(metadata)
    return why if standing == "stopped" else None


def unrunnable_successor_reason(metadata: dict) -> str | None:
    """Why a staged successor can no longer be admitted, or None when it still can.

    A CONFIRMED operator stop (``cancellation_reason``) forbids any successor: it is authority
    over execution and lands independently of this staging. UNKNOWN cancellation state retires
    nothing — retiring work on a guess is as destructive as admitting it (review 2026-09-18) —
    and it says nothing about the reasons below: a browser workflow that is gone is a confirmed
    reason of its own, and an unreadable stop record is no argument for keeping that row staged
    for ever.

    Beyond that, only browser successors depend on outside state between staging and
    admission: the operator can close the workflow, a follow-up can move its revision, a
    newer plan can replace it. Global A's compare-and-swap would raise on any of those, so
    the pressure gather retires such a successor loudly instead of admitting it.
    """
    cancelled = cancellation_reason(metadata)
    if cancelled:
        return cancelled
    if metadata.get("directive_intent") != "BROWSE":
        return None
    from meta_loop.browser.workflow import browser_workflow_receipt

    workflow_id = metadata.get("browser_workflow_id")
    receipt = browser_workflow_receipt(workflow_id) if workflow_id else None
    if receipt is None:
        return f"browser workflow {workflow_id!r} is gone"
    if receipt["status"] not in _RUNNABLE_WORKFLOW:
        return f"browser workflow {workflow_id!r} is {receipt['status']}"
    if receipt["revision"] != metadata.get("browser_workflow_revision"):
        return "browser workflow moved on (an operator turn or a newer control)"
    return None


# Run outcomes a successor may not continue: repeating them would fail the same way.
_UNCONTINUABLE_RUNS = frozenset({"blocked", "inference_failed", "busy", "note_queued"})


def stage_omega_successor(episode, control: OmegaControlDecision):
    """Build, announce and stage the successor in one step (no cognitive transaction)."""
    from pressure_loop.continuation import incoming
    from pressure_loop.pressure import complete_continuation, stage_reseed

    complete_continuation(incoming(episode))
    contributor = build_omega_successor(episode, control)
    if contributor is not None:
        if contributor not in stage_reseed([contributor]):
            return None
        announce_successor(control)
    return contributor


def name_lane_entry_mail(contributor, *, fold_id: str, continuation_key: str, outgoing) -> None:
    """Record, on the successor, the lane-entry mail its continuation must be handed (spec
    section 4, T9): the pinned ``mail_id``s with their ``effect_id``s and requirement, the
    ``continuation_key`` they are bound to, and the fold whose transaction record delivery
    is recorded against.

    ``outgoing`` is only ever mail for the lane this successor ENTERS: publication withholds
    every other injection as ``no_consumer`` rather than pinning mail no continuation can
    claim. Every value is host-minted; nothing here is model output. ``validate_successor``
    compares only its own keys, so successor identity is unchanged.
    """
    lane = str(contributor.metadata.get("omega_lane") or "")
    mail = [{"mail_id": str(row["mail_id"]), "effect_id": str(row["effect_id"]),
             "requirement": str(row["pin"]["requirement"])} for row in outgoing or ()]
    if mail:
        contributor.metadata["lane_entry"] = {
            "fold_id": str(fold_id), "continuation_key": str(continuation_key), "lane": lane,
            "mail": mail}


def announce_successor(control: OmegaControlDecision) -> None:
    """The one side effect of staging besides the ledger: a LEARN successor's chosen topic."""
    p = control.pressure
    if p is not None and p.intent == "LEARN" and p.topic:
        from pressure_loop.learn_topic_choice import set_chosen_topic

        # The LEARN this successor ignites studies the sub-aspect Omega named, not the domain overview.
        set_chosen_topic(p.domain or "", p.topic)


def build_omega_successor(episode, control: OmegaControlDecision):
    """The validated successor contributor, NOT yet staged — or None when there is none.

    Split from staging for the cognitive transaction (spec section 6, T8): the fold opens its
    durable record, which carries this row by value, BEFORE anything is staged, and staging
    then happens from the record (``pressure.complete_transaction``).
    """
    if type(control) is not OmegaControlDecision:
        raise ValueError("successor requires validated OmegaControlDecision")
    if control.decision == "DONE":
        return None

    parent = bounded_text(episode.episode_id, "episode_id", 128)
    identity = f"omega-successor-{uuid.uuid5(uuid.NAMESPACE_URL, parent).hex}"
    global_state = dict(getattr(episode, "global_", None) or {})
    packet = dict(global_state.get("omega_packet") or {})
    pressure = dict(getattr(episode, "pressure", None) or {})
    rows = pressure.get("contributors") or []
    origin = next((dict(row.get("detail") or {}) for row in rows
                   if row.get("id") == getattr(episode, "opened_by", None)), {})
    p = control.pressure
    if control.decision == "ASK_OPERATOR" and origin.get("directive_intent") == "ASK_OPERATOR":
        # This episode already put the question to the operator. Asking again before they
        # answer is a loop, not a choice; their reply is what continues the work.
        _decline(parent, "the question is already with the operator; their reply continues the loop")
        return None
    # An operator-originated parent carries the operator's request forward. An autonomous
    # parent (idle learning) has no request, so the successor pursues the state Omega
    # itself declared: its validated desired_state.
    objective = origin.get("original_objective") or packet.get("original_objective") or p.desired_state
    opener = str(getattr(episode, "opened_by", None) or "")
    # A window reflection's or an awakening's contributor carries its own cycle id; that is
    # not operator work and must not pass the operator-lineage gate (live 2026-09-15: a window
    # successor ran with idle learning paused).
    lineage = origin.get("original_cycle_id") or (origin.get("cycle_id") if opener in _OPERATOR_OPENERS else "")
    metadata = dict(successor_id=identity, cycle_id=identity, parent_episode_id=parent,
        source_episode_id=parent, original_cycle_id=lineage or "",
        # Spec section 1: the successor continues its parent's thread verbatim. A
        # parent that carries no thread (pre-T2) becomes the root of one rather
        # than starting its continuation as a second, unrelated root.
        thread_id=str(getattr(episode, "thread_id", "") or "") or parent,
        original_objective=objective, message=objective, directive_intent=p.intent,
        lane=omega_lane_for_intent(p.intent), omega_lane=omega_lane_for_intent(p.intent),
        reason=p.reason,
        desired_state=p.desired_state, observed_state=p.observed_state,
        resolution_conditions=list(p.resolution_conditions), omega_control=control.to_dict())
    if origin.get("lane_work_handoff") is True:
        lane = str(origin.get("lane") or "")
        handoff_id = str(origin.get("handoff_id") or "")
        if lane not in {"browser", "agent"} or not handoff_id:
            raise ValueError("specialist successor is missing its lane handoff identity")
        metadata.update(lane_work_handoff=True, handoff_id=handoff_id, lane=lane)
        if lane == "browser":
            workflow_id = str(origin.get("browser_workflow_id") or "").strip()
            if workflow_id:
                metadata["browser_workflow_id"] = workflow_id
    for key in ("domain", "topic"):
        if getattr(p, key) is not None:
            metadata[key] = getattr(p, key)
    if metadata["omega_lane"] == "agent" and metadata["original_cycle_id"]:
        observations = _agent_observations(episode, origin)
        if observations:
            metadata["agent_observations"] = observations
        run = dict(dict(dict(getattr(episode, "auto", None) or {}).get("outputs") or {}).get("agent_run") or {})
        if p.intent == "USE_TOOL" and str(run.get("status") or "") == "blocked":
            # Every call in the batch failed; the same batch fails the same way again.
            _decline(parent, "the agent batch was blocked; it cannot be continued")
            return None
        # The next batch re-owns the job this task already claimed instead of claiming a second
        # one on the same project, which its own conflict guard would refuse.
        job_id = str(run.get("job_id") or origin.get("agent_job_id") or "")
        if job_id:
            metadata["agent_job_id"] = bounded_text(job_id, "agent_job_id", 128)
            job_cycle_id = str(origin.get("job_cycle_id") or "").strip()
            if not job_cycle_id:
                from auto_loop.execute import job_control as jobs

                job_cycle_id = str(jobs.get_job(job_id).cycle_id or "").strip()
            if not job_cycle_id:
                raise ValueError(f"agent successor cannot resolve job cycle for {job_id}")
            metadata["job_cycle_id"] = job_cycle_id
    if metadata["original_cycle_id"] and origin.get("chat_bubble_waiting"):
        from pressure_loop.operator_chat_responses import get_response

        # Agent and browser work answers when Omega settles it, so the operator's bubble is
        # still waiting and the successor carries it. A chat reply already delivered is not
        # reopened.
        if get_response(metadata["original_cycle_id"]) is None:
            metadata["chat_bubble_waiting"] = True
    if p.intent == "BROWSE":
        from meta_loop.browser.workflow import browser_workflow_receipt, mark_browser_waiting

        workflow_id = dict(global_state.get("browser_workflow") or {}).get("workflow_id")
        # AUTO's run moved the durable revision after Global A bound the workflow, so the
        # successor names the revision as it is now; an operator turn in between retires it.
        receipt = browser_workflow_receipt(workflow_id) if workflow_id else None
        if receipt is None or receipt["status"] not in _RUNNABLE_WORKFLOW:
            _decline(parent, f"browser workflow {workflow_id!r} is "
                             f"{'gone' if receipt is None else receipt['status']}")
            return None
        outputs = dict(dict(getattr(episode, "auto", None) or {}).get("outputs") or {})
        run_status = str(dict(outputs.get("browser_run") or {}).get("status") or "")
        auto_failure = str(dict(getattr(episode, "auto", None) or {}).get("failure_kind") or "")
        if run_status in _UNCONTINUABLE_RUNS or auto_failure == "browser_perception_invalid":
            # A run that could not act, or a page that could not be perceived, fails the
            # same way on every retry; the workflow waits for the operator instead.
            mark_browser_waiting(workflow_id)
            _decline(parent, f"browser run {run_status or auto_failure} cannot be continued; the workflow waits for the operator")
            return None
        metadata.update(original_objective=str(receipt["objective"]), message=str(p.reason),
            browser_workflow_id=workflow_id, browser_workflow_revision=int(receipt["revision"]))
    cancelled = cancellation_reason(metadata)
    if cancelled:
        # The operator stopped this work while the fold was thinking about continuing it.
        # Publication is the first of the two checks (T12); admission is the second.
        _decline(parent, f"the operator stopped the work: {cancelled}")
        return None
    # UNKNOWN state does NOT decline here (review 2026-09-18). Returning None FINALIZES the
    # uncertainty in the durable record — published, ``staging: "none"``, the fold's required
    # injection withheld as ``no_consumer`` — and no later retry can undo that, so a transient
    # unreadable file permanently lost valid continuation work. The staged row IS the durable,
    # retryable obligation: it is published as usual, and ADMISSION is where the stop question
    # must be answered before anything runs. Publishing resurrects nothing, because nothing
    # runs without a confirmed-clean check at the gather and again at the lease.
    from pressure_loop.continuation import stamp

    stamp(metadata)
    validate_successor(metadata)
    # Confidence describes judgment, not whether this explicit decision is heard.
    return make_contributor("omega", "omega_successor", 1.0, metadata)


def stage_idle_learning_successor(*, intent: str, domain: str, topic: str, level: float, lever: str,
                                  reasoning: str, observed: str):
    """Idle learning's awakening enters pressure exactly like Omega's own PRESSURE decision.

    The awakening is Omega choosing to continue while the operator has idle learning engaged, so
    it stages one validated successor with no operator lineage (retired if idle learning pauses).
    """
    goal = (f"learn {topic} in {domain}" if intent == "LEARN"
            else f"apply {topic} in {domain} in working, tested code")
    why = str(reasoning or "").strip() or goal
    kind = "knowledge" if intent == "LEARN" else "application"
    assessment = dict(who="Omega", what=goal, when="idle learning window", where=domain, why=why,
                      how="one successor episode", desired_state=goal, observed_state=observed,
                      gap=why, evidence=[observed])
    control = OmegaControlDecision.from_dict(dict(
        assessment=assessment, decision="PRESSURE", synthesis=why,
        pressure=dict(intent=intent, confidence=float(level), reason=why, setpoints={lever: float(level)},
                      desired_state=goal, observed_state=observed, domain=domain, topic=topic,
                      resolution_conditions=[f"{kind} mastery in {domain} rises toward its target"])))
    parent = SimpleNamespace(episode_id=f"idle-awakening-{uuid.uuid4().hex}", global_={},
                             opened_by="idle_awakening", pressure={})
    return stage_omega_successor(parent, control)
