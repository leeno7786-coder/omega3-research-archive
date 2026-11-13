"""Re-enter held operator mail after its associated Coding Agent job settles.

Older admitted operator mail is delivered to the target job's lane while it is active. If
the job settles before a fold claims the message, this module returns the same words to the
durable ingress queue in their original conversation. Mail itself has no TTL; retries only
schedule another transfer attempt and never change delivery eligibility.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Mapping

log = logging.getLogger(__name__)

# Long enough to admit one turn durably; short enough that a crash mid-handoff returns the
# mail to a later sweep quickly.
_HANDOFF_LEASE_S = 300.0
_ORIGIN_PREFIX = "cross_lane_mail:"
# The chat lane offers no job tools, so words pinned there cannot restart a build.
_NO_RESTART_ROUTE = "FREEFORM_CHAT"
_NO_RESTART_STATUSES = frozenset({"stopped", "missing"})
# A failed transfer is retried after _RETRY_MIN_S, doubling to _RETRY_MAX_S; words that are
# only waiting — for a fold, or for a lease to lapse — are re-checked every _RETRY_MAX_S.
_RETRY_MIN_S = 5.0
_RETRY_MAX_S = 60.0
_CONTEXT_LIMIT = 500
_retry_lock = threading.Lock()
_retry_at: float | None = None
_retry_delay = _RETRY_MIN_S


def handoff_identity(mail_id: str) -> str:
    """The deterministic queue identity of one mail row's handoff (owned by the store)."""
    from global_loop.intersections import mail_handoff_identity

    return mail_handoff_identity(mail_id)


# Both purposes an operator's words may take when a build is running. Settled-job mail is
# re-entered as an ordinary turn; no elapsed time retires an admitted instruction.
_HELD_PURPOSES = frozenset({"operator_instruction", "stop_job"})


def _is_held_instruction(row: Mapping[str, Any]) -> bool:
    return (row.get("status") in {"ready", "leased"}
            and row.get("purpose") in _HELD_PURPOSES
            and row.get("delivery_point") == "omega_reason"
            and row.get("source_actor") == "operator"
            and bool(row.get("target_job_id")))


def _job(job_id: str):
    """``(status, record)``: "missing" when there is no record, None when unreadable."""
    from auto_loop.execute import job_control as jobs

    try:
        record = jobs.get_job(job_id)
    except KeyError:
        return "missing", None
    except Exception:  # noqa: BLE001 - an unreadable job is undecidable, never "settled"
        log.warning("could not read job %s while handing off held mail", job_id, exc_info=True)
        return None, None
    return record.status, record


def job_status(job_id: str) -> str | None:
    """The job's status; "missing" when it has no record; None when it cannot be read."""
    return _job(job_id)[0]


def _names_job(metadata: Mapping[str, Any], job_id: str) -> bool:
    return job_id in {str(metadata.get("job_id") or ""), str(metadata.get("agent_job_id") or "")}


def fold_pending(job_id: str) -> bool:
    """Whether a fold of this job may still bind its mail.

    A fold is ahead while the job is named by a staged successor in the durable ledger, by
    a queued turn (a boot restore, an answer carrying the job), or by a pipeline envelope
    in a state before COMMITTING — through its job id or the continuation it is running.
    Envelopes are stamped COMMITTING before the terminus, so an episode's own terminus sweep
    does not count its finished fold.

    **Checked upstream first.** Work moves ledger -> dispatch (READY envelope) and queue ->
    dispatch, and each downstream stage is published BEFORE the upstream one lets go
    (``mark_ready`` precedes both the ledger's release at episode open and the queue's
    acknowledgement). Reading the ledger and the queue before the envelopes therefore
    cannot miss work that moves between the reads.

    Undecidable state counts as pending: handing words off early would take them from the
    fold they were addressed to.
    """
    from core import pipeline_state
    from pressure_loop import cognition_queue as queue
    from pressure_loop.pressure import staged_signals, unpublished_job_continuation

    try:
        if unpublished_job_continuation(job_id):
            return True
        for contributor in staged_signals():
            if _names_job(dict(getattr(contributor, "metadata", None) or {}), job_id):
                return True
        for contributor in queue.peek_inputs():
            if _names_job(dict(getattr(contributor, "metadata", None) or {}), job_id):
                return True
        for envelope in pipeline_state.envelopes():
            if envelope.state not in pipeline_state.RESTORABLE_STATES:
                continue
            carried = dict(dict((envelope.continuation or {}).get("contributor") or {})
                           .get("metadata") or {})
            if envelope.job_id == job_id or _names_job(carried, job_id):
                return True
    except Exception:  # noqa: BLE001 - undecidable is pending; see docstring
        log.warning("could not tell whether job %s still folds", job_id, exc_info=True)
        return True
    return False


def _held_record(row: Mapping[str, Any], status: str, record) -> dict[str, str]:
    """The host's record of the build these words were held for, bounded."""
    def bounded(value: Any) -> str:
        return " ".join(str(value or "").split())[:_CONTEXT_LIMIT]

    return {
        "mail_id": str(row["mail_id"]),
        "job_id": str(row["target_job_id"]),
        "job_status": status,
        "objective": bounded(getattr(record, "objective", "")),
        "project_root": bounded(getattr(record, "project_root", "")),
        "summary": bounded(getattr(record, "summary", "")),
    }


def _turn(row: Mapping[str, Any], status: str, record):
    from pressure_loop.contributors import from_operator_message

    origin = dict(row.get("origin") or {})
    text = str(dict(row.get("payload") or {}).get("text") or "")
    if not text.strip():
        raise ValueError("held operator instruction carries no text")
    msg: dict[str, Any] = {
        "message": text,
        "ingress_class": str(origin.get("ingress_class") or ""),
        "weight": 1.0,
        "source_citation": dict(origin.get("source_citation") or {}),
        "cycle_id": str(origin.get("cycle_id") or row.get("source_episode_id") or ""),
        # Send order, so the arrival-ordered queue places it where the operator sent it.
        "arrival_seq": int(row.get("sent_seq") or 0) or int(row["arrival_seq"]),
        # That turn's bubble was already answered when the words were held.
        "chat_bubble_waiting": False,
        "thread_id": str(row["thread_id"]),
        "held_instruction": _held_record(row, status, record),
    }
    for key in ("chat_permission_mode", "chat_intent"):
        if origin.get(key):
            msg[key] = str(origin[key])
    if status in _NO_RESTART_STATUSES:
        msg["operator_route_pin"] = _NO_RESTART_ROUTE
    return from_operator_message(msg)


def _hand_off(row: Mapping[str, Any], status: str, record) -> bool:
    """Hand one row off. False when it is not ours to take now; raises on a real failure."""
    from global_loop import intersections as mail_store
    from pressure_loop import cognition_queue as queue

    mail_id = str(row["mail_id"])
    holder = mail_store.handoff_holder(mail_id)
    claimed = mail_store.claim_mail(
        delivery_point="omega_reason", lane="", thread_id=str(row["thread_id"]),
        episode_id=holder, lease_s=_HANDOFF_LEASE_S, job_id=str(row["target_job_id"]),
        mail_ids=[mail_id])
    if not claimed:
        # Behind an earlier message, or earlier words still in intake: a later sweep.
        return False
    identity = handoff_identity(mail_id)
    try:
        turn = _turn(claimed[0], status, record)
        # Ownership proven and the words admitted in ONE critical section of the mail store.
        admitted = mail_store.while_owned(
            claimed, episode_id=holder,
            action=lambda: queue.enqueue_handoff(turn, handoff_id=identity,
                                                 origin_ref=_ORIGIN_PREFIX + mail_id))
        # Retired only after the queue durably holds the words — now or on an earlier attempt.
        mail_store.acknowledge_mail(claimed, episode_id=holder)
    except BaseException:
        # Hand the lease back at once so the bounded retry resumes in order. If even that
        # fails, the lease lapses on its own and the store's claim keeps later words behind
        # this one meanwhile. Releasing is lenient: a lease another worker now holds is
        # left untouched.
        try:
            mail_store.release_mail(claimed, episode_id=holder)
        except Exception:  # noqa: BLE001 - the lease lapses instead
            log.warning("could not release the handoff lease on %s", mail_id, exc_info=True)
        raise
    queue.forget_handoff_receipts([identity])
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("pressure", "held_instruction_handoff", status="handed_off", ok=True,
                  summary="held operator words re-entered as an ordinary turn",
                  mail_id=mail_id, job_id=str(row["target_job_id"]), job_status=status,
                  admitted_now=admitted)
    except Exception:  # noqa: BLE001 - telemetry never fails a handoff
        pass
    return True


def _schedule_retry(*, outstanding: bool, failed: bool, progressed: bool,
                    now: float | None = None) -> None:
    """Schedule the next retry; wake pressure if it moves the attempt earlier."""
    global _retry_at, _retry_delay
    moment = time.time() if now is None else float(now)
    with _retry_lock:
        previous = _retry_at
        candidates: list[float] = []
        if not outstanding:
            _retry_delay = _RETRY_MIN_S
        else:
            if failed:
                delay = _RETRY_MIN_S if progressed else _retry_delay
                _retry_delay = min(_RETRY_MAX_S, delay * 2)
            else:
                delay, _retry_delay = _RETRY_MAX_S, _RETRY_MIN_S
            candidates.append(moment + delay)
        _retry_at = min(candidates) if candidates else None
        earlier = _retry_at is not None and (previous is None or _retry_at < previous)
    if earlier:
        _wake_pressure_loop()


def _wake_pressure_loop() -> None:
    """A rest bound only shapes waits that START after it; a loop already asleep must be told."""
    try:
        from pressure_loop.loop import notify_work

        notify_work()
    except Exception:  # noqa: BLE001 - the next rest still honours the bound
        log.warning("could not wake the pressure loop for a held-mail retry", exc_info=True)


def seconds_until_retry(now: float | None = None) -> float | None:
    """Seconds until held words are due another transfer attempt; None when none are owed."""
    moment = time.time() if now is None else float(now)
    with _retry_lock:
        return None if _retry_at is None else max(0.0, _retry_at - moment)


def retry_if_due(now: float | None = None) -> list[str]:
    """Run the sweep if a retry is due. Always leaves a retry scheduled while work remains."""
    due = seconds_until_retry(now)
    if due is None or due > 0.0:
        return []
    return hand_off_orphaned_instructions()


def request_retry() -> None:
    """Keep a failed durable handoff on the existing pressure retry schedule."""
    _schedule_retry(outstanding=True, failed=True, progressed=False)


def hand_off_orphaned_instructions() -> list[str]:
    """Hand off every held operator instruction whose job can no longer fold.

    Returns the mail ids handed off. A row whose job is still running, or whose settled job
    still has a fold ahead, is left for that fold. An undecidable job, a leased earlier row,
    or a failed transfer holds back the rest of its (thread, job) group. Anything left
    behind schedules a bounded retry; a store that cannot be read at all schedules one and
    propagates to the caller.
    """
    from global_loop import intersections as mail_store
    from pressure_loop import cognition_queue as queue

    handed: list[str] = []
    failed = False
    try:
        mail_store.release_expired_leases()
        every = mail_store.all_mail()
        from pressure_loop.specialist_lane_handoff import recover_pending_specialist_handoffs

        specialist_failures = recover_pending_specialist_handoffs(every)
        failed = bool(specialist_failures)
        rows = [row for row in every if _is_held_instruction(row)]
        rows.sort(key=lambda row: (int(row.get("sent_seq") or 0) or int(row["arrival_seq"]),
                                   int(row["arrival_seq"])))
        jobs: dict[str, tuple] = {}
        pending: dict[str, bool] = {}
        committed: dict[str, bool] = {}
        blocked: set[tuple[str, str]] = set()
        for row in rows:
            mail_id = str(row["mail_id"])
            job_id = str(row["target_job_id"])
            group = (str(row["thread_id"]), job_id)
            if job_id not in jobs:
                jobs[job_id] = _job(job_id)
            status, record = jobs[job_id]
            # Words already admitted belong to their ordinary turn whatever the job does now.
            committed[mail_id] = queue.handoff_accepted(handoff_identity(mail_id))
            if group in blocked:
                continue
            if not committed[mail_id] and status == "running":
                continue
            if row["status"] == "leased" or (status is None and not committed[mail_id]):
                # An earlier message still leased, or undecidable: nothing later may pass it.
                # Waiting, not failing: the retry re-checks.
                blocked.add(group)
                continue
            if not committed[mail_id]:
                if job_id not in pending:
                    pending[job_id] = fold_pending(job_id)
                if pending[job_id]:
                    continue
            try:
                if _hand_off(row, status or "unknown", record):
                    handed.append(mail_id)
                    continue
            except Exception:  # noqa: BLE001 - the mail stays; the retry resumes in order
                log.warning("held operator mail %s could not be handed off", mail_id,
                            exc_info=True)
            failed = True
            blocked.add(group)
        # Owed: every held row not handed off that no fold of a running job will take — its
        # job settled (a fold still ahead is re-checked in case it never comes), or its words
        # are already committed to their ordinary turn.
        remaining = [row for row in rows if str(row["mail_id"]) not in handed]
        outstanding = failed or any(
            committed[str(row["mail_id"])] or jobs[str(row["target_job_id"])][0] != "running"
            for row in remaining)
    except BaseException:
        _schedule_retry(outstanding=True, failed=True, progressed=bool(handed))
        raise
    _schedule_retry(outstanding=outstanding, failed=failed, progressed=bool(handed))
    if specialist_failures:
        raise ExceptionGroup("specialist handoff recovery failed", specialist_failures)
    return handed


def reconcile_handoffs_at_boot() -> list[str]:
    """Boot: retire orphan receipts and keep queued lane-handoff idempotency.

    A specialist receipt remains while its lane FIFO row is still present, even after the
    mail row is acknowledged. This preserves idempotency until the absorbed or executed queue
    copy is settled. Other source-retired receipts are pruned. No worker from before the
    restart can still be in flight.
    """
    from global_loop import intersections as mail_store
    from pressure_loop import cognition_queue as queue

    rows = mail_store.all_mail()
    live = {row["mail_id"] for row in rows}
    live_specialist = {
        str(dict(row.get("payload") or {}).get("handoff_id") or "")
        for row in rows if row.get("purpose") == "specialist_lane_handoff"
    }
    queued_specialist = {
        str(contributor.metadata.get("handoff_id") or "")
        for contributor in queue.peek_inputs()
        if contributor.type == "lane_work_dispatch"
        and contributor.metadata.get("lane_work_handoff") is True
    }
    from pressure_loop.specialist_lane_handoff import SPECIALIST_ORIGIN_PREFIX

    stale = []
    for identity, receipt in queue.handoff_receipts().items():
        origin_ref = receipt["origin_ref"]
        if (origin_ref.startswith(_ORIGIN_PREFIX)
                and origin_ref[len(_ORIGIN_PREFIX):] not in live):
            stale.append(identity)
        elif (origin_ref.startswith(SPECIALIST_ORIGIN_PREFIX)
              and identity not in live_specialist
              and identity not in queued_specialist):
            stale.append(identity)
    queue.forget_handoff_receipts(stale)
    return hand_off_orphaned_instructions()


def reset_retry_for_tests() -> None:
    global _retry_at, _retry_delay
    with _retry_lock:
        _retry_at, _retry_delay = None, _RETRY_MIN_S


__all__ = ["fold_pending", "hand_off_orphaned_instructions",
           "handoff_identity", "job_status", "reconcile_handoffs_at_boot", "request_retry", "retry_if_due",
           "seconds_until_retry"]
