"""pressure_loop.loop — the single pressure tick loop + tick counter.

Owns ONLY pressure: each tick gathers contributors and evaluates them; on
ignition it hands off to the cognition chain (``cognition_chain.ignite_episode``)
and pauses until the episode completes, then resumes ticking. The handoff and the
cognition cascade live OUTSIDE the pressure loop.

NOTE: this tick driver is transitional — it relocates as the loops are reorganized.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from guardian.terminal_feedback import phase_span
from core.cognition_pacing import between_tick_seconds
from pressure_loop.policy import pressure_open_threshold

log = logging.getLogger("omega.pressure_loop.loop")

_stop_event = threading.Event()
# The idle loop sleeps on this instead of polling every tick. Operator input sets it
# (via notify_work, wired into the operator-input enqueue) so a command wakes the mind
# instantly instead of waiting out the idle window; stop sets it too.
_work_event = threading.Event()


def notify_work() -> None:
    """Wake the idle loop NOW — operator input arrived (or we're stopping). Cheap and
    idempotent; the loop clears the flag each iteration before it processes work."""
    _work_event.set()


def _retry_held_mail() -> None:
    """Retry handing off held operator words when due (cross-lane T7). Never fails a tick."""
    try:
        from pressure_loop.held_instruction_handoff import retry_if_due

        retry_if_due()
    except Exception:  # noqa: BLE001 - the words stay durable and the retry stays scheduled
        log.warning("held operator mail retry failed; rescheduled", exc_info=True)


def _rest_bound(timeout: float) -> float:
    """An idle rest never outlasts a scheduled handoff retry of held operator words.

    Without the bound a failed transfer on an otherwise idle system waited for some
    unrelated event, possibly until the words expired (review 2026-09-16).
    """
    try:
        from pressure_loop.held_instruction_handoff import seconds_until_retry

        due = seconds_until_retry()
    except Exception:  # noqa: BLE001 - an unreadable schedule never blocks the loop
        return timeout
    return timeout if due is None else max(0.0, min(float(timeout), due))


def reset_pressure_loop_stop() -> None:
    """Clear the cooperative stop flag (tests / ``--once`` smoke)."""
    _stop_event.clear()


def request_pressure_loop_stop() -> None:
    """Ask ``run_pressure_tick_loop`` to exit cleanly after the current tick."""
    _stop_event.set()
    _work_event.set()  # interrupt any idle sleep so the loop notices the stop at once


def pressure_loop_stop_requested() -> bool:
    return _stop_event.is_set()


def _write_pause_state(runtime, state: dict[str, object]) -> None:
    try:
        runtime.shared_state.write("pressure_loop_paused", bool(state.get("paused")))
        runtime.shared_state.write("_pressure_loop_pause", state)
    except Exception:
        log.warning("pressure pause shared-state write failed", exc_info=True)


def _log_tick_summary(shared_state, admitted: bool) -> None:
    """One observability line per TICK — not per lane (cut 8).

    ``ignited`` stays a single boolean: the tick either put cognition in flight
    or it did not. Which lanes lit up rides the evaluate phase span
    (``lanes_offered`` / ``lanes_busy`` / ``lanes_ignited``), where the detail
    belongs.
    """
    try:
        tick = int(shared_state.read("_pressure_tick") or 0)
    except Exception:
        tick = 0
    try:
        from guardian.pressure_observability import log_tick_summary

        log_tick_summary(tick=tick, ignited=admitted)
    except Exception:
        pass


def _pressure_signature() -> tuple:
    """Everything that could ignite right now, as a comparable identity.

    The loop rests on "nothing dispatched". That is only a safe rest if nothing
    was THERE — and on 2026-08-15 it was not: a tick found live pressure,
    dispatched nothing because two signals collided at the ignition gate, and
    the loop slept on top of a mind that still wanted to act. It stayed asleep
    for 7m23s because the events that would have woken it (the episode close)
    had already fired BEFORE that tick.

    So rest is decided on STATE, not on the outcome of one pass: if the pressure
    picture is unchanged since the last pass that dispatched nothing, there is
    genuinely nothing new to act on and resting is right. If it changed — a
    signal emitted, a setpoint raised, an operator turn queued — the mind gets
    another pass immediately instead of waiting for an unrelated event to
    supply one.
    """
    try:
        from pressure_loop.cognition_queue import peek_inputs
        from pressure_loop.pressure import staged_signals

        staged = tuple(
            sorted((str(c.type), round(float(c.weight), 4)) for c in staged_signals())
        )
        queued = len(tuple(peek_inputs()))
        return (staged, queued)
    except Exception:  # noqa: BLE001 -- an unreadable picture must never wedge the loop
        log.debug("pressure signature unavailable; treating as changed", exc_info=True)
        return (uuid.uuid4().hex,)


def _repass_while_pressure_changes(runtime, last_signature):
    """Re-evaluate PRESSURE while the picture keeps changing. Returns (signature, ignited).

    Deliberately re-runs only ``_run_pressure_tick_body`` — not the whole tick.
    Calendar wake, workspace sync, the job scheduler and the tick/epoch writes
    are once-per-tick by contract, and re-entering the loop body to re-check
    pressure would fire those side effects two and three times per tick.

    A pass is taken only when the picture actually CHANGED, so a steady state
    costs one comparison rather than a gather, and the bound stops a signal that
    is live but permanently unignitable from spinning the loop.
    """
    signature = last_signature
    for _ in range(_MAX_IDLE_REPASSES):
        current = _pressure_signature()
        if current == signature:
            return current, False  # nothing new — resting here is correct
        signature = current
        if _stop_event.is_set():
            return signature, False
        with phase_span("pressure", "repass", episode_id="no-episode"):
            if _run_pressure_tick_body(runtime):
                return signature, True
    return signature, False


def _decay_tonic() -> None:
    """Idle-tick tonic decay — exactly ONCE per tick, never once per lane.

    Cut 8 offers up to four ignition contests per tick; the tonic is a single
    organism-wide chemistry, so running its decay per lane would age it up to
    four times as fast on a quiet tick. Decay still fires only when NOTHING
    ignited anywhere — an igniting tick is not an idle tick no matter which
    lane lit up.
    """
    try:
        from pressure_loop.pressure_field import decay_neuromod_tonic

        decay_neuromod_tonic()
    except Exception:
        log.warning("neuromod tonic decay failed", exc_info=True)


def _set_ignition_span(span, ignition) -> None:
    """Record the ignition's shape on the evaluate phase span."""
    dom = ignition.dominant_contributor
    extra: dict[str, object] = {
        "dominant": dom.type,
        "agg": round(float(ignition.aggregate_pressure), 3),
    }
    ranked = sorted((float(c.weight) for c in ignition.contributors), reverse=True)
    if len(ranked) >= 2 and ranked[1] > 0:
        extra["margin"] = round(ranked[0] / ranked[1], 3)
    span.set(**extra)


def _run_pressure_tick_body(runtime) -> bool:
    """Stage eligible internal work, then fill physical capacity from one FIFO."""
    from dataclasses import replace
    from core.pipelines import work_kind_for_contributor
    from pressure_loop.cognition_queue import (
        enqueue_input, release_input_lease, contributor_dedup_key,
    )
    from pressure_loop.pipeline_dispatch import (
        dispatch_cascade, free_pipeline_ids, pipeline_faults, raise_pending_fatal,
    )
    from pressure_loop.pressure import evaluate, gather_contributors, retry_unresolved_stops, settle_retired_jobs

    raise_pending_fatal()
    # An operator stop whose durable write failed holds in memory and is retried here, so the
    # authority becomes durable as soon as the ledger takes it (T12).
    retry_unresolved_stops()
    try:
        settle_retired_jobs()
    except Exception:
        log.warning("retired job settlement will retry on the next tick", exc_info=True)
    store, shared_state = runtime.episode_store, runtime.shared_state
    contributors = gather_contributors(shared_state, episode_store=store)
    # A standing source gets one durable admission until it disappears. Queue
    # identity also deduplicates a retry before acknowledgement.
    present = {contributor_dedup_key(c) for c in contributors}
    _STAGED_INTERNAL.intersection_update(present)
    for contributor in contributors:
        key = contributor_dedup_key(contributor)
        if key in _STAGED_INTERNAL:
            continue
        eligible = evaluate([contributor], active_episode=False, shared_state=shared_state,
                            episode_store=store, open_threshold=pressure_open_threshold())
        if eligible is not None:
            enqueue_input(contributor, operator_activity=False)
            _STAGED_INTERNAL.add(key)
    admitted = False
    # Four permanent semantic lanes each lease only their own oldest queued item.
    # Each free physical pipeline is permanently bound to one semantic lane. Queue
    # selection is lane-local, so an empty or held Chat line cannot stop Browser,
    # Coding Agent, or Window from advancing. Capacity is read once per pass, so a
    # worker finishing mid-pass frees its lane for the NEXT pass; that is nonpreemption.
    assignable = [pid for pid in free_pipeline_ids()
                  if store is None or store.current(pipeline_id=pid) is None]
    for pipeline_id in assignable:
        contributor, deferred = _lease_runnable_input(pipeline_id)
        if deferred:
            # An unknown cancellation state is NOT a cancellation and NOT an empty
            # lane. The row keeps its lane position, but other semantic lanes are
            # independent and may advance during this pass.
            continue
        if contributor is None:
            continue
        try:
            work_kind = work_kind_for_contributor(contributor)
            ignition = evaluate([contributor], active_episode=False, shared_state=shared_state,
                                episode_store=store, open_threshold=pressure_open_threshold())
            if ignition is None:
                release_input_lease(pipeline_id)
                continue
            assigned = replace(ignition, pipeline_id=pipeline_id, work_kind=work_kind)
            dispatch_cascade(
                store, assigned, contributor, pipeline_id=pipeline_id,
                work_kind=assigned.work_kind,
                on_not_admitted=lambda pid=pipeline_id: release_input_lease(pid),
            )
            admitted = True
        except BaseException as exc:
            release_input_lease(pipeline_id)
            if pipeline_faults().get(pipeline_id) is exc:
                log.error(
                    "pipeline %s is quarantined; its FIFO head remains durable while "
                    "other lanes continue: %s",
                    pipeline_id,
                    exc,
                )
                continue
            raise
    if not admitted:
        _decay_tonic()
    _log_tick_summary(shared_state, admitted)
    return admitted


def _retire_stopped_lane_handoff(contributor, pipeline_id: int, reason: str) -> bool:
    """Retire a stopped specialist item and its prompt mail before either can be replayed."""
    from global_loop import intersections as mail_store
    from pressure_loop.cognition_queue import acknowledge_input, forget_handoff_receipts, release_input_lease
    from pressure_loop.operator_chat_responses import append_lane_update

    metadata = dict(contributor.metadata)
    lane = str(metadata.get("lane") or "")
    handoff_id = str(metadata.get("handoff_id") or "")
    cycle_id = str(metadata.get("original_cycle_id") or "")
    thread_id = str(metadata.get("thread_id") or "")
    mail_id = str(metadata.get("mail_id") or "")
    if lane not in {"browser", "agent"} or not handoff_id or not cycle_id or not thread_id:
        raise ValueError("stopped_specialist_handoff_identity_invalid")

    mail_store.release_expired_leases()
    mail_row = next((row for row in mail_store.all_mail() if row.get("mail_id") == mail_id), None)
    if mail_row is not None and mail_row["status"] == "leased":
        release_input_lease(pipeline_id)
        return False
    if mail_row is not None and mail_row["status"] == "ready":
        holder = f"stopped-{handoff_id}"
        claimed = mail_store.claim_mail(
            delivery_point="omega_reason", lane=lane, thread_id=thread_id,
            episode_id=holder, lease_s=60, job_id=str(metadata.get("agent_job_id") or ""),
            mail_ids=[mail_id],
        )
        if not claimed:
            release_input_lease(pipeline_id)
            return False
        if mail_store.acknowledge_mail(claimed, episode_id=holder) != 1:
            raise RuntimeError(f"stopped specialist mail {mail_id} was not acknowledged")

    lane_name = "Browser" if lane == "browser" else "Coding Agent"
    append_lane_update(
        cycle_id=cycle_id,
        episode_id=f"stopped-{handoff_id}",
        lane=lane,
        handoff_id=handoff_id,
        status="failed",
        text=f"The {lane_name} lane was stopped before it started. Cause: {reason}".strip(),
        update_id=f"{handoff_id}:stopped",
    )
    if not acknowledge_input(contributor, pipeline_id):
        raise RuntimeError(f"stopped specialist handoff {handoff_id} lost its queue lease")
    forget_handoff_receipts([handoff_id])
    return True


def _lease_runnable_input(pipeline_id: int):
    """Lease the next input, retiring settled duplicates and stopped continuations.

    Returns ``(contributor, deferred)``. ``deferred`` is True when a continuation was left in
    place because its cancellation state is UNKNOWN — which is NOT an empty queue, and the
    caller must not read it as one: the two are different answers and the deferral must not be
    mistaken for "nothing is waiting" (review 2026-09-18).

    Selection is lane-local; this adds no semantic routing filter of its own. An
    unknown cancellation holds this lane, while other lanes are still admitted.

    A specialist update consumed by an already-running fold leaves its durable FIFO copy in
    place until mail acknowledgement. The copy is then acknowledged without starting another
    episode; an interrupted mail acknowledgement is reconciled from the durable mail row.

    Cancellation is rechecked HERE, at actual admission (spec section 11, T12). The gather
    retires the STAGED row, but the FIFO holds its own copy: a stop that landed after the
    successor was enqueued left that copy to reach ``dispatch_cascade`` on a later tick and
    resurrect the stopped work. Releasing the lease would only postpone it — the row keeps its
    queue position — so for a CONFIRMED stop the exact leased item is ACKNOWLEDGED, which
    removes it durably, and its staged row is retired so replaying the staging is a no-op.
    Keeps leasing so one stopped continuation cannot cost this pipeline its pass.
    """
    from pressure_loop.cognition_queue import (
        acknowledge_input,
        contributor_dedup_key,
        lease_next_input,
        release_input_lease,
    )
    from pressure_loop.omega_successor import cancellation_state
    from pressure_loop.pressure import continuation_standing, retire_successor

    while True:
        contributor = lease_next_input(pipeline_id)
        if contributor is None:
            return contributor, False
        if contributor.type == "lane_work_dispatch":
            handoff_id = str(contributor.metadata.get("handoff_id") or "")
            if contributor.metadata.get("mail_absorbed"):
                if not acknowledge_input(contributor, pipeline_id):
                    raise RuntimeError(f"absorbed specialist handoff {handoff_id} lost its queue lease")
                from pressure_loop.cognition_queue import forget_handoff_receipts

                forget_handoff_receipts([handoff_id])
                continue
            if contributor.metadata.get("mail_absorption_pending"):
                # Crash recovery for the queue/mail two-store handoff. Mail still present
                # means acknowledgement did not commit: a ready row can be handled by this
                # lane episode, while a live lease keeps the FIFO head in place. Missing mail
                # proves the previous fold acknowledged it, so this dispatch is only residue.
                from global_loop import intersections as mail_store

                mail_id = str(contributor.metadata.get("mail_id") or "")
                if not mail_id:
                    raise ValueError("pending_specialist_handoff_mail_id_required")
                mail_row = next((row for row in mail_store.all_mail()
                                 if str(row.get("mail_id") or "") == mail_id), None)
                if mail_row is None:
                    if not acknowledge_input(contributor, pipeline_id):
                        raise RuntimeError(
                            f"settled specialist handoff {handoff_id} lost its queue lease"
                        )
                    from pressure_loop.cognition_queue import forget_handoff_receipts

                    forget_handoff_receipts([handoff_id])
                    continue
                if mail_row.get("status") == "leased":
                    release_input_lease(pipeline_id)
                    return None, True
            head = str(contributor.metadata.get("handoff_id") or contributor_dedup_key(contributor))
            standing, reason = cancellation_state(contributor.metadata)
            if standing != "unknown":
                _UNJUDGEABLE_SINCE.pop(head, None)
            if standing == "unknown":
                return _defer_unjudgeable_head(
                    head, reason, pipeline_id, release_input_lease
                )
            if not standing:
                return contributor, False
            if _retire_stopped_lane_handoff(contributor, pipeline_id, reason):
                continue
            return None, True
        if contributor.type != "omega_successor":
            return contributor, False
        head = str(contributor.metadata.get("successor_id") or contributor_dedup_key(contributor))
        standing, reason = cancellation_state(contributor.metadata)
        if not standing:
            standing, reason = continuation_standing(contributor.metadata)
        if standing != "unknown":
            _UNJUDGEABLE_SINCE.pop(head, None)
        if not standing:
            return contributor, False
        if standing == "unknown":
            # Not a cancellation: an unreadable record must not consume work nobody stopped
            # (review 2026-09-18). The lease is released so the row keeps its queue position,
            # and the caller is told this was a DEFERRAL — the operator's line still moves,
            # and the next tick judges this row again.
            return _defer_unjudgeable_head(
                head, reason, pipeline_id, release_input_lease
            )
        # Consume the queued copy FIRST: that is what keeps the stopped work out of the
        # cascade. Retiring the staged row is bookkeeping the gather repeats on its next pass,
        # so a ledger that cannot be written must not take the tick down with it — and must
        # never leave the copy in the queue because the second write failed.
        acknowledge_input(contributor, pipeline_id)
        try:
            retire_successor(contributor, reason)
        except Exception:  # noqa: BLE001 - the copy is already gone; the gather retires the row
            log.warning("could not retire the stopped continuation %s at admission",
                        contributor.metadata.get("successor_id"), exc_info=True)


def _defer_unjudgeable_head(head: str, reason: str, pipeline_id: int, release_lease):
    """Keep an unjudgeable FIFO head durable, then fail loudly if it holds the lane too long."""
    from core.required_substrate import RequiredSubstrateError

    log.warning("deferring lane head whose cancellation state is unknown: %s", reason)
    release_lease(pipeline_id)
    now = _clock()
    waited = max(0.0, now - _UNJUDGEABLE_SINCE.setdefault(head, now))
    if waited >= UNJUDGEABLE_HEAD_LIMIT_S:
        raise RequiredSubstrateError(
            "pressure",
            "admission",
            reason="a queued lane head's cancellation authority cannot be read",
            detail=(f"{head}: {reason}; unjudgeable for {waited:.0f}s, and nothing "
                    "behind it can be admitted until it is"),
        )
    return None, True


_STAGED_INTERNAL: set[tuple[str, str]] = set()

#: How long one queue head may stay unjudgeable — its cancellation authority unreadable — before
#: admission fails loudly instead of holding the whole line behind it in silence.
UNJUDGEABLE_HEAD_LIMIT_S = 120.0
#: When each unjudgeable head was first deferred, by successor id. Process-local: a restart
#: re-judges from scratch, and the bound restarts with it.
_UNJUDGEABLE_SINCE: dict[str, float] = {}


def _clock() -> float:
    return time.monotonic()


# MIND-DRIVEN CYCLES: when the mind keeps igniting, chain episodes back-to-back with no
# clock delay; a brief breath after a long uninterrupted burst lets pause/stop interpose
# + the chip cool (each cascade already paces in seconds).
_MAX_CHAIN_BURST = 25
_CHAIN_BREATH_SECONDS = 1.0
# How many passes an UNCHANGED pressure picture gets before the loop rests.
# >1 so a lane freeing between the pass and the rest is still caught; small
# because an unchanged picture that has not ignited twice will not ignite on the
# hundredth try, and re-passing costs a full gather.
_MAX_IDLE_REPASSES = 2


def run_pressure_tick_loop(runtime) -> None:
    """The ONLY loop. Advance pressure once per tick; on ignition hand off + pause."""
    ss = runtime.shared_state
    tick = 0
    chain_burst = 0
    # The pressure picture at the last pass that dispatched nothing, and how many
    # passes it has been unchanged for. Rest is decided against these, not
    # against one pass's outcome.
    last_idle_signature: tuple | None = None
    last_pause_signature: tuple[str, ...] | None = None
    log.info(
        "pressure loop: event-driven (mind-driven chaining + idle-wake timer; no fixed auto-tick)"
    )
    while not _stop_event.is_set():
        # Held operator words are durable admission, not cognition: their retry runs even
        # while cognition is paused, since the queue holds the line during a pause anyway.
        _retry_held_mail()
        # Fail-fast: the pause-read GATES whether cognition runs. If it can't be
        # read it must NOT default to "not paused" and tick — that would run
        # cognition while the dev/CLI agent holds the runtime locked on opus, and
        # cognition would fail. An unreadable gate halts the loop.
        from pressure_loop.pressure_loop_pause import pressure_loop_pause_state

        pause_state = pressure_loop_pause_state()

        if pause_state.get("paused"):
            owners = tuple(str(o) for o in (pause_state.get("owners") or []))
            if owners != last_pause_signature:
                log.info("pressure loop paused by %s", ", ".join(owners) or "unknown")
                last_pause_signature = owners
                try:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "pressure",
                        "pressure_loop_pause",
                        status="ok",
                        summary=f"tick paused owners={list(owners)}",
                        paused=True,
                        owners=list(owners),
                    )
                except Exception:
                    pass
            _write_pause_state(runtime, pause_state)
            time.sleep(between_tick_seconds())
            continue
        if last_pause_signature is not None:
            log.info("pressure loop resumed")
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "pressure_loop_pause",
                    status="ok",
                    summary="tick resumed",
                    paused=False,
                )
            except Exception:
                pass
            last_pause_signature = None
            _write_pause_state(runtime, pause_state)

        tick += 1
        # Clear the work signal BEFORE we process: operator input enqueued from here on
        # re-sets it, so an idle sleep below can never miss a command (lost-wakeup safe).
        _work_event.clear()
        # Fail-fast cognition: a tick failure (SharedState write or the cognition
        # cascade) propagates and HALTS the loop — a swallowed failure rolled into
        # the next tick is a mind running on poison. phase_span emits "fail" first.
        ss.write("_pressure_tick", tick)
        ss.write("_epoch", tick)
        with phase_span("pressure", "tick", episode_id="no-episode", tick=tick):
            ignited = _run_pressure_tick_body(runtime)
        # MIND-DRIVEN CYCLES: Omega controls the autonomous loop, not a clock. An episode
        # ignited (the mind raised pressure) -> CHAIN the next cycle immediately, no sleep,
        # so the system keeps acting on the task until the mind chooses idle. A bounded
        # burst forces a brief breath for pause/stop.
        if ignited:
            chain_burst += 1
            if chain_burst < _MAX_CHAIN_BURST:
                continue
            chain_burst = 0
            if _stop_event.wait(timeout=_CHAIN_BREATH_SECONDS):
                break
            continue
        # ── NOTHING DISPATCHED. That is not the same as "the mind went quiet".
        #
        # A pass can dispatch nothing while pressure is still live: every lane
        # busy, a leased head held, an ignition-gate refusal. Resting on the
        # OUTCOME of one pass parks a mind that still wants to act, and the
        # events that would wake it (the episode close, the signal emission)
        # have already fired — so it sleeps until something UNRELATED arrives.
        # Measured 2026-08-15: 7m23s asleep on live 0.85 pressure, resumed only
        # because the operator happened to start a build.
        #
        # So rest is decided on STATE: while the pressure picture keeps changing
        # this re-evaluates PRESSURE ONLY, right here, without going round the
        # loop. That distinction matters — the tick body above (calendar,
        # workspace sync, job scheduler, the tick/epoch writes) is once-per-tick
        # by contract, and re-entering it to re-check pressure would run those
        # side effects two and three times per tick. Bounded so a live-but-
        # unignitable signal cannot spin hot (the 69,273 phantom-ignition
        # lesson in _release_lane_lease).
        if not ignited:
            last_idle_signature, ignited = _repass_while_pressure_changes(
                runtime, last_idle_signature
            )
        if ignited:
            chain_burst = 0
            continue

        # ── IDLE: the mind went quiet. Run the idle TIMER (operator 2026-07-09) instead
        # of polling every tick — no gather/evaluate churn while resting, so the terminal
        # stays readable. On idle expiry (5 min since the operator last spoke, throttled)
        # fire the wake, which records pressure the NEXT cycle ignites; the mind reignites
        # from there for as long as it wants. Operator input wakes this sleep instantly.
        chain_burst = 0
        from pressure_loop import idle_wake

        if not idle_wake.governor_enabled():
            # Idle learning is OFF: the mind acts ONLY on real triggers — operator
            # input (notify_work) or stop — never a fixed-clock re-poll that wakes to
            # re-run gather/evaluate and find nothing (operator 2026-07-21: kill the
            # automatic tick doing nothing). A due calendar reminder still bounds the
            # wait so a scheduled commitment fires with autonomy off (mirrors the
            # governor-ON path below); absent one, block until an event arrives.
            #
            # The bound is the idle-recheck window, NOT the wake timer: with the
            # governor off the wake never fires, so `seconds_until_wake_attempt`
            # reads 0.0 forever once the idle window passes and this wait
            # returns instantly — a hot spin that ran the tick ~60 times a
            # second and buried the terminal (live 2026-09-07).
            _work_event.wait(timeout=_rest_bound(idle_wake.seconds_until_idle_recheck()))
            if _stop_event.is_set():
                break
            continue
        if idle_wake.wake_due() and idle_wake.wake_attempt_allowed():
            idle_wake.fire_awakening()  # decide-and-record; next cycle ignites it
            continue
        # Not due yet (or throttled): sleep until the wake can next fire.
        _idle_timeout = _rest_bound(idle_wake.seconds_until_wake_attempt())
        _work_event.wait(timeout=_idle_timeout)
        if _stop_event.is_set():
            break
    if _stop_event.is_set():
        log.info("pressure loop: stopped cleanly")


def run_pressure_tick_once(runtime) -> None:
    """Single pressure tick (smoke / --once); no sleep."""
    ss = runtime.shared_state
    ss.write("_pressure_tick", 1)
    ss.write("_epoch", 1)
    with phase_span("pressure", "tick", episode_id="no-episode", tick=1):
        _run_pressure_tick_body(runtime)
