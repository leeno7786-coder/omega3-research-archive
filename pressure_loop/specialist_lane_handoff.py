"""Chat Global A's specialist route becomes durable work on its own lane FIFO.

An operator turn always enters the Chat lane. When Global A routes it to Browser or the
Coding Agent, the host stages that request here as a ``lane_work_dispatch`` contributor and
admits it through the ONE ingress gate, ``cognition_queue.enqueue_handoff``. The target lane
then starts it as its own pressure-opened episode; the Chat turn is acknowledged only after
this admission returns.

**Deterministic and idempotent.** The handoff identity is minted from lane, thread and the
original chat cycle, so a retry of the same routed turn — even after its first copy was
leased and consumed — is answered from the queue's durable receipt as already accepted,
never admitted twice.

**Recoverable publication.** Mail durably records the frozen queue input before admission.
It cannot be claimed until its queue receipt exists. A retry or boot completes interrupted
admission using that same input and recalled context. Failures preserve their causes; no
model is called and nothing carries a TTL.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from pressure_loop import cognition_queue
from pressure_loop.contributors import make_contributor

# Only the specialist lanes Global A hands off to; Chat keeps its own turn and Window
# reflection is host-minted, never an operator route.
SPECIALIST_LANE_INTENTS: Mapping[str, str] = {"browser": "BROWSE", "agent": "RESPOND"}
LANE_WORK_TYPE = "lane_work_dispatch"
SPECIALIST_ORIGIN_PREFIX = "operator_cycle:"
# Leaves the origin reference inside the queue's 256-character bound.
_IDENTITY_LIMIT = 200
_RUN_MEMORY_LIMIT = 8


def _identity(name: str, value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"lane_work_{name}_required")
    if len(value) > _IDENTITY_LIMIT:
        raise ValueError(f"lane_work_{name}_too_long")
    return value


def _lane(value: Any) -> str:
    if type(value) is not str or value not in SPECIALIST_LANE_INTENTS:
        raise ValueError(f"lane_work_lane_not_specialist: {value!r}")
    return value


def lane_work_handoff_id(lane: str, thread_id: str, cycle_id: str) -> str:
    """The deterministic 64-hex queue identity of one routed turn's lane handoff."""
    parts = (LANE_WORK_TYPE, _lane(lane), _identity("thread_id", thread_id),
             _identity("cycle_id", cycle_id))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _workflow_id(lane: str, browser_workflow: Any) -> str | None:
    if browser_workflow is None:
        return None
    if lane != "browser":
        raise ValueError("lane_work_browser_workflow_outside_browser_lane")
    if not isinstance(browser_workflow, Mapping):
        raise ValueError("lane_work_browser_workflow_invalid")
    return _identity("browser_workflow_id", browser_workflow.get("workflow_id"))


def _active_run_memory(
    lane: str, thread_id: str, request: str
) -> tuple[str, str, str, dict[str, Any]]:
    """Recall only the host-resolved active Coding Agent run for this conversation."""
    if lane != "agent":
        return "", "", "", {"status": "not_applicable", "evidence": []}

    from auto_loop.execute import job_control

    active = [job for job in job_control.active_jobs()
              if job.status == "running" and job.thread_id == thread_id]
    if not active:
        return "", "", "", {"status": "no_active_run", "evidence": []}
    if len(active) != 1:
        raise ValueError(f"coding_run_thread_ambiguous: {thread_id}")
    job = active[0]
    job_cycle_id = str(job.cycle_id or "").strip()
    if not job_cycle_id:
        raise ValueError(f"coding_run_cycle_id_missing: {job.job_id}")
    job_objective = str(job.objective or "").strip()
    if not job_objective:
        raise ValueError(f"coding_run_objective_missing: {job.job_id}")
    from auto_loop.mril_tools.run_scoped import recall_for_active_run

    context = recall_for_active_run(
        job_id=job.job_id,
        thread_id=thread_id,
        instruction_revision=int(job.applied_revision),
        operator_update=request,
        allowed_lobes=(8, 11),
        limit=_RUN_MEMORY_LIMIT,
    )
    if not isinstance(context, Mapping) or context.get("status") not in {"verified", "empty"}:
        raise ValueError("coding_run_memory_recall_returned_invalid_result")
    return str(job.job_id), job_cycle_id, job_objective, dict(context)


def _stable_id(prefix: str, handoff_id: str, length: int) -> str:
    return hashlib.sha256(f"{prefix}:{handoff_id}".encode("utf-8")).hexdigest()[:length]


def _fit_run_memory_to_mail(mail_row: dict[str, Any]) -> None:
    """Keep the longest verified evidence prefix that fits durable mail and Omega.

    MRIL has already verified every returned record. Prompt membership is a separate
    decision: keep complete records in their recall order and name every omitted ID.
    The operator's instruction, route context, run anchor and proof fields are never
    truncated. This selection is persisted with the mail, so retries use it unchanged.
    """
    from global_loop.intersections import payload_fits
    from omega_loop.episode.packet import omega_mail_fits

    payload = mail_row["payload"]
    # Validate the complete payload before trimming anything: schema failures
    # must survive even if their evidence row would be omitted for capacity.
    storage_fits = payload_fits(payload)
    prompt_fits = omega_mail_fits([mail_row])
    if storage_fits and prompt_fits:
        return
    reason = "omega_prompt_capacity" if not prompt_fits else "mail_payload_capacity"
    run_memory = payload["run_memory"]
    evidence = list(run_memory.get("evidence") or ())
    for count in range(len(evidence) - 1, -1, -1):
        included = evidence[:count]
        payload["run_memory"] = {
            **run_memory,
            "evidence": included,
            "evidence_selection": {
                "retrieved_count": len(evidence),
                "included_count": count,
                "omitted_memory_ids": [row["memory_id"] for row in evidence[count:]],
                "reason": reason,
            },
        }
        mail_row["provenance"] = [f"memory:{row['memory_id']}" for row in included]
        if payload_fits(payload) and omega_mail_fits([mail_row]):
            return
    raise ValueError("specialist_lane_mail_exceeds_omega_prompt_capacity")


def _admit_recorded_mail(row: Mapping[str, Any]) -> dict[str, str]:
    """Replay the input frozen with the mail, never resolve a different active run."""
    payload = row["payload"]
    lane = _lane(row["target_lane"])
    thread_id = _identity("thread_id", row["thread_id"])
    cycle_id = _identity("cycle_id", row["source_episode_id"])
    identity = lane_work_handoff_id(lane, thread_id, cycle_id)
    metadata = dict(row["origin"].get("lane_work") or {})
    metadata["message"] = payload["text"]
    metadata["operator_route_context"] = payload["route_context"]
    if row["origin"].get("objective_from_request") is True:
        metadata["original_objective"] = payload["text"]
    expected = {
        "cycle_id": identity, "lane": lane, "handoff_id": identity,
        "original_cycle_id": cycle_id, "thread_id": thread_id,
        "mail_id": row["mail_id"], "message": payload.get("text"),
        "operator_route_context": payload.get("route_context"), "lane_work_handoff": True,
    }
    if (payload.get("handoff_id") != identity
            or row["mail_id"] != _stable_id("specialist-lane-mail", identity, 16)
            or any(metadata.get(key) != value for key, value in expected.items())):
        raise ValueError("specialist_lane_handoff_recovery_record_invalid")
    origin_ref = SPECIALIST_ORIGIN_PREFIX + cycle_id
    try:
        admitted = cognition_queue.enqueue_handoff(
            make_contributor("omega", LANE_WORK_TYPE, 1.0, metadata),
            handoff_id=identity, origin_ref=origin_ref,
        )
    except Exception:
        from pressure_loop.held_instruction_handoff import request_retry

        request_retry()
        raise
    return {
        "status": "queued" if admitted else "already_accepted",
        "handoff_id": identity, "lane": lane, "original_cycle_id": cycle_id,
        "thread_id": thread_id, "origin_ref": origin_ref, "mail_id": row["mail_id"],
        "run_memory_status": str(payload["run_memory"]["status"]),
        "active_job_id": str(metadata.get("agent_job_id") or ""),
    }


def recover_pending_specialist_handoffs(rows: list[dict[str, Any]]) -> list[Exception]:
    """Recover independent addresses, retaining failures and per-address order."""
    accepted = cognition_queue.handoff_receipts()
    failures: list[Exception] = []
    blocked: set[tuple[str, str, str]] = set()
    for row in sorted(rows, key=lambda item: int(item.get("arrival_seq") or 0)):
        if row.get("purpose") != "specialist_lane_handoff":
            continue
        group = (str(row.get("target_lane") or ""), str(row.get("thread_id") or ""),
                 str(row.get("target_job_id") or ""))
        if group in blocked:
            continue
        try:
            identity = str(row["payload"].get("handoff_id") or "")
            if identity not in accepted:
                _admit_recorded_mail(row)
        except Exception as exc:
            exc.add_note(f"specialist mail {row.get('mail_id')!r} awaiting admission")
            failures.append(exc)
            blocked.add(group)
    return failures


def stage_operator_lane_work(
    *,
    lane: str,
    request: str,
    cycle_id: str,
    thread_id: str,
    route_context: str,
    arrival_seq: int = 0,
    browser_workflow: Mapping[str, Any] | None = None,
    selected_skill: str | None = None,
) -> dict[str, str]:
    """Admit a Global-A-approved Browser or Coding Agent request to its lane FIFO.

    Returns a bounded receipt whose ``status`` is ``queued`` when admitted now, or
    ``already_accepted`` when this same routed turn was admitted before. Raises
    ``ValueError`` for an invalid handoff and propagates any queue failure unchanged.
    """
    lane = _lane(lane)
    if type(request) is not str or not request.strip():
        raise ValueError("lane_work_request_required")
    cycle_id = _identity("cycle_id", cycle_id)
    thread_id = _identity("thread_id", thread_id)
    if type(arrival_seq) is not int or arrival_seq < 0:
        raise ValueError("lane_work_arrival_seq_invalid")
    if type(route_context) is not str:
        raise ValueError("lane_work_route_context_invalid")
    if selected_skill is not None:
        if lane != "agent":
            raise ValueError("lane_work_selected_skill_outside_agent_lane")
        if type(selected_skill) is not str or not selected_skill.strip() or len(selected_skill) > 128:
            raise ValueError("lane_work_selected_skill_invalid")
        selected_skill = selected_skill.strip()
    workflow_id = _workflow_id(lane, browser_workflow)

    handoff_id = lane_work_handoff_id(lane, thread_id, cycle_id)
    origin_ref = SPECIALIST_ORIGIN_PREFIX + cycle_id
    mail_id = _stable_id("specialist-lane-mail", handoff_id, 16)
    effect_id = _stable_id("specialist-lane-effect", handoff_id, 32)
    accepted_receipt = {
        "status": "already_accepted", "handoff_id": handoff_id, "lane": lane,
        "original_cycle_id": cycle_id, "thread_id": thread_id, "origin_ref": origin_ref,
        "mail_id": mail_id, "run_memory_status": "already_accepted", "active_job_id": "",
    }
    existing = cognition_queue.handoff_receipts().get(handoff_id)
    if existing is not None:
        if existing.get("origin_ref") != origin_ref:
            raise ValueError("specialist_lane_handoff_identity_conflict")
        return accepted_receipt
    from global_loop import intersections as mail_store

    if mail_store.mail_was_acknowledged(mail_id=mail_id, effect_id=effect_id):
        return accepted_receipt
    prior_mail = next((row for row in mail_store.all_mail() if row["mail_id"] == mail_id), None)
    if prior_mail is not None:
        frozen = prior_mail["payload"]
        if (frozen.get("text") != request or frozen.get("route_context") != route_context
                or frozen.get("selected_skill") != selected_skill
                or frozen.get("browser_workflow_id") != workflow_id):
            raise ValueError("specialist_lane_handoff_retry_conflict")
        return _admit_recorded_mail(prior_mail)
    job_id, job_cycle_id, original_objective, run_memory = _active_run_memory(
        lane, thread_id, request
    )
    # Mail is prompt context, not a tool invocation envelope. MRIL's local
    # response labels the recall operation as ``tool``; rename that descriptive
    # provenance field before crossing the mailbox schema, which rejects tool
    # authority keys recursively.
    run_memory = dict(run_memory)
    recall_tool = run_memory.pop("tool", None)
    if recall_tool is not None:
        run_memory["recall_method"] = recall_tool
    payload: dict[str, Any] = {
        "lane": lane,
        "text": request,
        "route_context": route_context,
        "handoff_id": handoff_id,
        "original_cycle_id": cycle_id,
        "run_memory": run_memory,
    }
    if selected_skill is not None:
        payload["selected_skill"] = selected_skill
    if workflow_id is not None:
        payload["browser_workflow_id"] = workflow_id

    mail_row = {
        "mail_id": mail_id,
        "purpose": "specialist_lane_handoff",
        "requirement": "required",
        "source_actor": "operator",
        "source_episode_id": cycle_id,
        "arrival_seq": arrival_seq or 10 ** 18,
        "payload": payload,
        "provenance": [f"memory:{row['memory_id']}" for row in run_memory.get("evidence", ())
                       if isinstance(row, Mapping) and row.get("memory_id")],
    }
    _fit_run_memory_to_mail(mail_row)

    metadata: dict[str, Any] = {
        # The lane episode's own cycle. The original chat turn was already resolved by its
        # AUTO acknowledgement, so later lane reports correlate through original_cycle_id.
        "cycle_id": handoff_id,
        "lane": lane,
        "directive_intent": (
            "USE_SKILL" if lane == "agent" and selected_skill is not None
            else SPECIALIST_LANE_INTENTS[lane]
        ),
        "operator_route": lane,
        "operator_route_context": route_context,
        "lane_work_handoff": True,
        "handoff_id": handoff_id,
        "original_cycle_id": cycle_id,
        "original_objective": original_objective or request,
        "thread_id": thread_id,
        "message": request,
        "mail_id": mail_id,
    }
    if selected_skill is not None:
        metadata["selected_skill"] = selected_skill
    if job_id:
        metadata["agent_job_id"] = job_id
        metadata["job_id"] = job_id
        metadata["job_cycle_id"] = job_cycle_id
    if workflow_id is not None:
        metadata["browser_workflow_id"] = workflow_id
    # Derive duplicated text from the same durable payload at recovery. Keeping
    # only the distinct control fields avoids doubling the mailbox's size bound.
    recorded = dict(metadata)
    del recorded["message"], recorded["operator_route_context"]
    objective_from_request = recorded["original_objective"] == request
    if objective_from_request:
        del recorded["original_objective"]
    # The returned durable row is authoritative, including if a concurrent retry
    # pinned this deterministic identity first. No consumer sees it before admission.
    try:
        pinned = mail_store.pin_mail(
            thread_id=thread_id, delivery_point="omega_reason",
            purpose="specialist_lane_handoff", requirement="required",
            source_actor="operator", source_episode_id=cycle_id,
            payload=payload, target_lane=lane, target_job_id=job_id,
            provenance=mail_row["provenance"], sent_seq=arrival_seq,
            origin={"cycle_id": cycle_id, "ingress_class": "specialist_lane_handoff",
                    "lane_work": recorded, "objective_from_request": objective_from_request},
            mail_id=mail_id, effect_id=effect_id,
        )
    except Exception:
        from pressure_loop.held_instruction_handoff import request_retry

        request_retry()
        raise
    if pinned is None:
        return accepted_receipt
    return _admit_recorded_mail(pinned)


__all__ = [
    "LANE_WORK_TYPE",
    "SPECIALIST_LANE_INTENTS",
    "lane_work_handoff_id",
    "stage_operator_lane_work",
]
