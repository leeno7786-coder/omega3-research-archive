"""Four stable physical workers; queue acknowledgement never ends occupancy.

A cascade that raises belongs to ONE physical pipeline, and it QUARANTINES that
pipeline and nothing else. The other three keep their capacity, so a failing
agent or browser lane no longer ends every conversation for the life of the
process. The failure is not softened on the way: it is relayed to the durable
FAILED envelope (``mark_failed``) exactly as before, kept readable in-process
through ``pipeline_faults()``, and logged with its cause intact.

Quarantine, not release. ``cognition_chain._ignite_episode_cascade``
deliberately exits its exception path still HOLDING that pipeline's cognition
lock, so a failed lane is not provably reusable — handing it new work would
park the next worker on an acquire that never returns, which is a silent hang
and strictly worse than the latch this replaces. The lane's occupancy is
released (it is no longer in flight, and shutdown accounting stays honest)
while the lane itself stays closed to new work.

Everything NOT confined to the failing pipeline stays fail-hard for the whole
process, exactly as before: releasing the shared queue lease, releasing the
durable continuation ledger, and persisting the FAILED envelope all touch state
the other three pipelines read, so when one of those fails lane isolation
cannot be proven and admission latches. The same holds once every pipeline is
quarantined — with no isolated capacity left there is nothing to isolate into,
and carrying on would be a silent stop, so the accumulated faults become the
process fatal.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Callable

from core.pipeline_state import mark_failed, mark_ready
from core.pipelines import (
    PIPELINE_IDS,
    lane_for_contributor,
    lane_for_pipeline,
    validate_pipeline_id,
)

log = logging.getLogger("omega.pressure.pipeline_dispatch")

_LOCK = threading.RLock()
_EXECUTORS: dict[int, ThreadPoolExecutor] = {}
_THREADS: dict[int, threading.Thread] = {}
_FUTURES: dict[int, Future] = {}
_IN_FLIGHT: set[int] = set()
# The cascade failure that closed each pipeline, in the order the pipelines failed.
_LANE_FAULTS: dict[int, BaseException] = {}
_FATAL: BaseException | None = None
_STOPPING = False


def notify_work() -> None:
    from pressure_loop.loop import notify_work as wake
    wake()


def _relay(exc: BaseException) -> None:
    global _FATAL
    with _LOCK:
        if _FATAL is None:
            _FATAL = exc
        elif _FATAL is not exc:
            _FATAL.add_note(f"Additional pipeline failure: {exc!r}")


def _answer_chat_if_admission_failed(
    contributor, pipeline_id: int, work_kind: str, exc: BaseException
) -> None:
    """Resolve the originating bubble if a turn fails before its episode can open.

    The close-time cognition path can answer failures only after an Episode exists. Admission
    failures happen before that path, but the queue contributor already carries the same
    originating cycle and waiting-bubble contract. ``append_response`` is idempotent, so this
    also safely covers an exception raised after the episode's own failure close ran.
    """
    metadata = dict(getattr(contributor, "metadata", None) or {})
    cycle_id = str(metadata.get("cycle_id") or "").strip()
    if not cycle_id or not metadata.get("chat_bubble_waiting"):
        return
    try:
        from pressure_loop.operator_chat_responses import append_response

        detail = " ".join(str(exc).split())[:500]
        text = f"I could not start this turn ({type(exc).__name__})"
        if detail:
            text += f": {detail}"
        if not text.endswith("."):
            text += "."
        append_response(
            cycle_id=cycle_id,
            episode_id="",
            text=text,
            delivery={"failed": True},
            pipeline_id=pipeline_id,
            work_kind=work_kind,
        )
    except Exception:
        log.exception("could not resolve chat bubble after episode admission failure")


def _quarantine(pipeline_id: int, exc: BaseException) -> None:
    """Close ONE pipeline over its own cascade failure, preserving the cause.

    Recorded before the worker's ``finally`` gives the occupancy back, so there is no
    instant at which a capacity read can see this pipeline as free again.
    """
    pid = validate_pipeline_id(pipeline_id)
    with _LOCK:
        _LANE_FAULTS.setdefault(pid, exc)
        log.error("pipeline %s quarantined by its cascade failure", pid, exc_info=exc)
        if set(_LANE_FAULTS) >= set(PIPELINE_IDS):
            # No isolated capacity is left to serve anything, so silence here would be a
            # stop nobody was told about. Halt on the accumulated causes instead.
            for fault in _LANE_FAULTS.values():
                _relay(fault)


def pipeline_faults() -> dict[int, BaseException]:
    """The cascade failure that closed each quarantined pipeline; never a silent empty."""
    with _LOCK:
        return dict(_LANE_FAULTS)


def raise_pending_fatal() -> None:
    with _LOCK:
        # Relaying a failure that is NOT confined to one pipeline must never reopen
        # admission. Only the test reset clears this process-lifetime latch after
        # workers drain. A single lane's cascade failure does not reach here: it
        # quarantines its own pipeline and leaves the rest of the mind running.
        exc = _FATAL
    if exc is not None:
        raise exc


def free_pipeline_ids() -> tuple[int, ...]:
    with _LOCK:
        if _STOPPING or _FATAL is not None:
            return ()
        return tuple(pid for pid in PIPELINE_IDS
                     if pid not in _IN_FLIGHT and pid not in _LANE_FAULTS)


def pipeline_in_flight(pipeline_id: int) -> bool:
    pid = validate_pipeline_id(pipeline_id)
    with _LOCK:
        return pid in _IN_FLIGHT


def _executor_for(pipeline_id: int) -> ThreadPoolExecutor:
    pid = validate_pipeline_id(pipeline_id)
    with _LOCK:
        if pid not in _EXECUTORS:
            _EXECUTORS[pid] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pipeline-{pid}")
        return _EXECUTORS[pid]


def dispatch_cascade(store, ignition, contributor, *, pipeline_id: int,
                     work_kind: str, on_not_admitted: Callable[[], None]) -> None:
    pid = validate_pipeline_id(pipeline_id)
    if ignition.pipeline_id != pid or ignition.work_kind != work_kind:
        raise ValueError("dispatch requires the exact physically bound ignition")
    if ignition.dominant_contributor is not contributor:
        raise ValueError("dispatch contributor must be the leased ignition contributor")
    if lane_for_contributor(contributor) != lane_for_pipeline(pid):
        raise ValueError(
            f"dispatch contributor does not belong to pipeline {pid}'s semantic lane"
        )

    def run() -> None:
        from cognition_chain import ignite_episode
        admitted = False
        with _LOCK:
            _THREADS[pid] = threading.current_thread()
        try:
            admitted = ignite_episode(store, ignition) is not None
        except BaseException as exc:
            # This pipeline's cascade, and only this pipeline's. Close the lane before
            # anything below can give its capacity back.
            _quarantine(pid, exc)
            _answer_chat_if_admission_failed(contributor, pid, work_kind, exc)
            try:
                from pressure_loop.pressure import release_continuation

                release_continuation(contributor.metadata, f"the cascade failed: {exc}")
            except BaseException as persistence_exc:
                # The successor ledger is read by all four pipelines. A failure to write it
                # is not this lane's to carry, so it stays fail-hard for the process.
                _relay(persistence_exc)
            try:
                mark_failed(pid, exc)
            except BaseException as persistence_exc:
                # Without the durable FAILED envelope the cause does not survive this
                # process, and nothing downstream can prove the lane was isolated.
                _relay(persistence_exc)
        finally:
            try:
                if not admitted:
                    on_not_admitted()
            except BaseException as exc:
                # The shared FIFO may still believe this lease is held: not one lane's.
                _relay(exc)
            finally:
                try:
                    # Measurement only (demo bar B2): the lane this turn occupied is free again.
                    # Marked HERE, in the finally that actually frees it, so a cascade that
                    # raised still records the capacity coming back.
                    from pressure_loop.turn_latency import mark

                    mark(
                        str(dict(getattr(contributor, "metadata", None) or {}).get("cycle_id")
                            or ""),
                        "released",
                        pipeline_id=pid,
                        work_kind=work_kind or None,
                    )
                except Exception:
                    pass
                with _LOCK:
                    _IN_FLIGHT.remove(pid)
                notify_work()

    rejected: BaseException | None = None
    with _LOCK:
        if _FATAL is not None:
            rejected = _FATAL
        elif pid in _LANE_FAULTS:
            # A capacity snapshot taken before this pipeline failed must not be able to
            # publish READY on it. The lane's own cause is what the caller gets.
            rejected = _LANE_FAULTS[pid]
        elif _STOPPING or pid in _IN_FLIGHT:
            raise RuntimeError(f"pipeline {pid} is unavailable")
        else:
            _IN_FLIGHT.add(pid)
            try:
                # Publication is mandatory and precedes any possible queue acknowledgement.
                mark_ready(pid, ignition)
                _FUTURES[pid] = _executor_for(pid).submit(run)
            except BaseException:
                try:
                    on_not_admitted()
                finally:
                    _IN_FLIGHT.remove(pid)
                raise
    if rejected is not None:
        # The caller has already leased a queue item based on its earlier capacity read.
        # Return that lease outside the dispatcher lock, then surface the lane/process cause.
        try:
            on_not_admitted()
        except BaseException as release_error:
            _relay(release_error)
            raise
        raise rejected


def shutdown_pipelines(timeout_s: float = 30.0) -> dict:
    from core.cognition_halt import request_halt
    global _STOPPING
    request_halt("runtime_teardown")
    with _LOCK:
        _STOPPING = True
        pools = list(_EXECUTORS.values())
        threads = list(_THREADS.items())
        futures = list(_FUTURES.items())
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=False)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    if futures:
        wait([future for _, future in futures], timeout=max(0.0, deadline - time.monotonic()))
    for _, thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return {"pools": len(pools), "waited_for": sorted({pid for pid, _ in threads + futures}),
            "still_running": sorted({pid for pid, thread in threads if thread.is_alive()}
                                    | {pid for pid, future in futures if not future.done()})}


def shutdown_for_tests(wait: bool = True) -> None:
    global _STOPPING, _FATAL
    with _LOCK:
        _STOPPING = True
        pools = list(_EXECUTORS.values())
    for pool in pools:
        pool.shutdown(wait=wait)
    with _LOCK:
        if _IN_FLIGHT:
            raise RuntimeError("cannot reset dispatch while workers are running")
        _EXECUTORS.clear()
        _THREADS.clear()
        _FUTURES.clear()
        _LANE_FAULTS.clear()
        _STOPPING = False
        _FATAL = None
