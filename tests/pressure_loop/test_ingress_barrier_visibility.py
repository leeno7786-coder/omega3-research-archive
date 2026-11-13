"""The ingress barrier must be visible while it holds the line (demo track, H4).

`lease_next_input` passes over the whole queue while any EARLIER arrival is still in ingress:

    if _head_blocked_by_reservation(item):
        return None

That barrier is correct — it is what stops a later arrival overtaking a turn that already has its
`arrival_seq` but has not finished screening (AGENTS.md section 4.0, strict FCFS). What is wrong is
that it is **invisible and unbounded from inside the live process**:

* `_prune_expired_reservations_locked` only retires reservations whose `process_epoch` differs from
  this one, so the 60 s TTL cleans up after a RESTART and never rescues a running process;
* a reservation whose ingress hangs or whose thread dies therefore stops leasing on **all four**
  pipelines for as long as the process lives;
* and nothing anywhere records that it is happening, so the symptom is "Omega went quiet" with no
  cause on any channel.

A visitor cannot tell a stalled queue from a thinking one, which is the exact failure the
no-silent-stall rule exists to prevent. This suite pins the barrier's observability: while it holds,
it says so, names which arrival holds it, and says for how long.
"""

from __future__ import annotations

import pytest

from pressure_loop import cognition_queue as cq
from pressure_loop.contributors import make_contributor


@pytest.fixture(autouse=True)
def _clean():
    cq._reset_cache_for_tests()
    yield
    cq._reset_cache_for_tests()


def _queued(seq: int, cycle: str = ""):
    return make_contributor(
        "autonomous", "curiosity", 1.0,
        {"arrival_seq": seq, "cycle_id": cycle or f"auto-{seq}", "lane": "chat"},
    )


def test_an_unblocked_queue_reports_no_barrier():
    assert cq.enqueue_input(_queued(2), operator_activity=False) is True

    state = cq.ingress_barrier_state()
    assert state["blocked"] is False
    assert state["arrival_seq"] is None
    assert state["held_for_s"] is None, "nothing is held, so there is no duration to report"
    assert cq.lease_next_input(0) is not None


def test_while_an_earlier_arrival_is_in_ingress_the_barrier_names_it():
    """The line is held for arrival 1, so arrival 2 waits — and the hold is reportable."""
    reserved = cq.reserve_operator_arrival("cyc-ingress")
    assert cq.enqueue_input(_queued(reserved + 1), operator_activity=False) is True

    assert cq.lease_next_input(0) is None, "a later arrival must not overtake an earlier ingress"
    state = cq.ingress_barrier_state()
    assert state["blocked"] is True
    assert state["arrival_seq"] == reserved
    assert state["cycle_id"] == "cyc-ingress"
    assert state["held_for_s"] is not None and state["held_for_s"] >= 0.0
    assert state["process_local"] is True, (
        "a reservation from THIS process never expires, so its hold is unbounded"
    )
    assert state["pipelines_blocked"] == 1


def test_the_barrier_clears_when_the_ingress_completes():
    reserved = cq.reserve_operator_arrival("cyc-done")
    assert cq.enqueue_input(_queued(reserved + 1), operator_activity=False) is True
    assert cq.lease_next_input(0) is None

    cq.abandon_arrival_reservation(reserved, cycle_id="cyc-done")

    assert cq.ingress_barrier_state()["blocked"] is False
    assert cq.lease_next_input(0) is not None, "the line moves again once ingress finishes"


def test_a_hold_beyond_its_budget_is_reported_once_with_its_cause():
    """The loud half: a barrier that outlives its budget is emitted as an attributable stall,
    once per barrier rather than once per poll, so the cause reaches a channel a human reads."""
    emitted: list[dict] = []
    cq.set_ingress_stall_reporter(lambda payload: emitted.append(dict(payload)))
    try:
        reserved = cq.reserve_operator_arrival("cyc-slow")
        assert cq.enqueue_input(_queued(reserved + 1), operator_activity=False) is True
        cq._force_ingress_barrier_age_for_tests(cq.INGRESS_STALL_BUDGET_S + 1.0)

        for pipeline in (0, 1, 2, 3):
            assert cq.lease_next_input(pipeline) is None
        assert len(emitted) == 1, "one report per barrier, not one per poll"
        report = emitted[0]
        assert report["arrival_seq"] == reserved
        assert report["cycle_id"] == "cyc-slow"
        assert report["held_for_s"] >= cq.INGRESS_STALL_BUDGET_S
        assert report["process_local"] is True
        assert "ingress" in report["reason"]
    finally:
        cq.set_ingress_stall_reporter(None)


def test_a_new_barrier_after_a_reported_one_is_reported_again():
    emitted: list[dict] = []
    cq.set_ingress_stall_reporter(lambda payload: emitted.append(dict(payload)))
    try:
        first = cq.reserve_operator_arrival("cyc-a")
        assert cq.enqueue_input(_queued(first + 1), operator_activity=False) is True
        cq._force_ingress_barrier_age_for_tests(cq.INGRESS_STALL_BUDGET_S + 1.0)
        assert cq.lease_next_input(0) is None
        cq.abandon_arrival_reservation(first, cycle_id="cyc-a")
        first_row = cq.lease_next_input(0)
        assert first_row is not None
        assert cq.acknowledge_input(first_row, 0) is True

        second = cq.reserve_operator_arrival("cyc-b")
        assert cq.enqueue_input(_queued(second + 1), operator_activity=False) is True
        cq._force_ingress_barrier_age_for_tests(cq.INGRESS_STALL_BUDGET_S + 1.0)
        assert cq.lease_next_input(0) is None

        assert [row["cycle_id"] for row in emitted] == ["cyc-a", "cyc-b"]
    finally:
        cq.set_ingress_stall_reporter(None)


def test_a_reporter_that_raises_never_breaks_the_lease_path():
    """Telemetry is not a dependency of admission (section 2.2)."""
    def _explode(_payload):
        raise RuntimeError("no channel")

    cq.set_ingress_stall_reporter(_explode)
    try:
        reserved = cq.reserve_operator_arrival("cyc-boom")
        assert cq.enqueue_input(_queued(reserved + 1), operator_activity=False) is True
        cq._force_ingress_barrier_age_for_tests(cq.INGRESS_STALL_BUDGET_S + 1.0)
        assert cq.lease_next_input(0) is None, "the barrier still holds, and nothing raised"
    finally:
        cq.set_ingress_stall_reporter(None)


def test_the_barrier_state_is_a_read_and_never_moves_the_line():
    reserved = cq.reserve_operator_arrival("cyc-read")
    assert cq.enqueue_input(_queued(reserved + 1), operator_activity=False) is True

    for _ in range(5):
        assert cq.ingress_barrier_state()["blocked"] is True
    assert cq.lease_next_input(0) is None, "reading the state did not consume or release anything"
    assert cq.ingress_barrier_state()["arrival_seq"] == reserved


def test_the_latency_read_reports_the_barrier_beside_the_turns():
    """One place answers "why was that slow?": the turn's own marks, and whether the line was
    held by an ingress that had not finished."""
    import asyncio
    import json

    from hud.server import create_app

    reserved = cq.reserve_operator_arrival("cyc-telemetry")
    assert cq.enqueue_input(_queued(reserved + 1), operator_activity=False) is True
    assert cq.lease_next_input(0) is None

    endpoint = next(
        route.endpoint for route in create_app().routes
        if getattr(route, "path", "") == "/api/telemetry/turn-latency"
    )
    body = json.loads(asyncio.run(endpoint(limit=5)).body)
    assert body["ingress_barrier"]["blocked"] is True
    assert body["ingress_barrier"]["arrival_seq"] == reserved
    assert body["ingress_barrier"]["cycle_id"] == "cyc-telemetry"
