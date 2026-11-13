"""Operator-turn latency marks — the measurement B2 needs (demo track, 2026-09-22).

The acceptance bar names a number: median time-to-first-audio under 2 s on a conversational turn
while two lanes are leased. Nothing in this system measured that. The live four-pipeline evidence
from 2026-09-06 *contained* the numbers inside its occupancy trace and nobody wrote them down, and
no test anywhere asserts a latency budget.

This is the instrument. It is diagnostic only: a bounded in-memory ring, no persistence, no
canonical memory, no second store of episode state — and deliberately not a gate. A missing mark
reads as **unknown**, never as zero, because a latency of 0 ms is the most flattering lie this
substrate could tell about itself (AGENTS.md section 2.1).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean():
    from pressure_loop import turn_latency

    turn_latency.reset_for_tests()
    yield
    turn_latency.reset_for_tests()


def test_the_phases_are_declared_in_the_order_a_turn_passes_through_them():
    from pressure_loop import turn_latency

    assert turn_latency.TURN_PHASES == (
        "arrival", "admitted", "first_token", "reply", "released",
    )


def test_a_turn_reports_each_phase_relative_to_its_own_arrival():
    from pressure_loop import turn_latency

    clock = iter([100.0, 100.8, 102.5, 104.7, 120.0])
    turn_latency.mark("cyc-1", "arrival", now=next(clock))
    turn_latency.mark("cyc-1", "admitted", now=next(clock))
    turn_latency.mark("cyc-1", "first_token", now=next(clock))
    turn_latency.mark("cyc-1", "reply", now=next(clock))
    turn_latency.mark("cyc-1", "released", now=next(clock))

    snap = turn_latency.snapshot("cyc-1")
    assert snap["cycle_id"] == "cyc-1"
    assert snap["since_arrival_ms"] == {
        "arrival": 0, "admitted": 800, "first_token": 2500, "reply": 4700, "released": 20000,
    }
    assert snap["queue_wait_ms"] == 800
    assert snap["time_to_first_token_ms"] == 2500
    assert snap["time_to_reply_ms"] == 4700
    assert snap["capacity_held_ms"] == 19200
    assert snap["after_reply_ms"] == 15300
    assert snap["complete"] is True


def test_a_missing_phase_is_unknown_and_never_zero():
    from pressure_loop import turn_latency

    turn_latency.mark("cyc-2", "arrival", now=10.0)
    turn_latency.mark("cyc-2", "admitted", now=10.4)

    snap = turn_latency.snapshot("cyc-2")
    assert snap["since_arrival_ms"]["first_token"] is None
    assert snap["time_to_first_token_ms"] is None, "an unmeasured latency must not read as fast"
    assert snap["time_to_reply_ms"] is None
    assert snap["complete"] is False
    assert snap["missing"] == ["first_token", "reply", "released"]


def test_a_turn_nobody_marked_is_absent_rather_than_instant():
    from pressure_loop import turn_latency

    assert turn_latency.snapshot("never-seen") is None
    assert turn_latency.recent() == []


def test_a_phase_keeps_its_first_observation():
    """A retried generation restarts the token stream; the turn did not get faster because of it."""
    from pressure_loop import turn_latency

    turn_latency.mark("cyc-3", "arrival", now=0.0)
    turn_latency.mark("cyc-3", "first_token", now=3.0)
    turn_latency.mark("cyc-3", "first_token", now=9.0)

    assert turn_latency.snapshot("cyc-3")["time_to_first_token_ms"] == 3000


def test_a_mark_that_arrives_before_its_arrival_is_recorded_as_unknown_not_negative():
    from pressure_loop import turn_latency

    turn_latency.mark("cyc-4", "admitted", now=5.0)     # no arrival mark yet
    turn_latency.mark("cyc-4", "arrival", now=7.0)      # out of order

    snap = turn_latency.snapshot("cyc-4")
    assert snap["since_arrival_ms"]["admitted"] is None, "a negative latency is not a measurement"
    assert "admitted" in snap["out_of_order"]


def test_an_unknown_phase_name_is_refused_rather_than_silently_counted():
    from pressure_loop import turn_latency

    with pytest.raises(ValueError, match="phase"):
        turn_latency.mark("cyc-5", "thinking_hard", now=1.0)


def test_a_mark_with_no_cycle_id_is_dropped_and_never_raises():
    from pressure_loop import turn_latency

    assert turn_latency.mark("", "arrival", now=1.0) is False
    assert turn_latency.mark(None, "arrival", now=1.0) is False  # type: ignore[arg-type]
    assert turn_latency.recent() == []


def test_recent_returns_newest_first_and_is_bounded():
    from pressure_loop import turn_latency

    for index in range(turn_latency.RING_SIZE + 5):
        turn_latency.mark(f"cyc-{index:04d}", "arrival", now=float(index))

    rows = turn_latency.recent()
    assert len(rows) == turn_latency.RING_SIZE, "the ring is bounded"
    assert rows[0]["cycle_id"] == f"cyc-{turn_latency.RING_SIZE + 4:04d}", "newest first"
    assert turn_latency.snapshot("cyc-0000") is None, "the oldest turn was evicted"
    assert turn_latency.recent(limit=3) == rows[:3]


def test_the_summary_reports_a_median_and_the_population_it_came_from():
    """A median over an unstated population is the defect WO-11 exists to prevent."""
    from pressure_loop import turn_latency

    for index, (admitted, token) in enumerate(((0.5, 1.0), (0.5, 2.0), (0.5, 9.0))):
        cycle = f"m-{index}"
        turn_latency.mark(cycle, "arrival", now=0.0)
        turn_latency.mark(cycle, "admitted", now=admitted)
        turn_latency.mark(cycle, "first_token", now=token)
    turn_latency.mark("m-incomplete", "arrival", now=0.0)  # no first token: excluded, not zero

    summary = turn_latency.summary()
    assert summary["time_to_first_token_ms"]["median"] == 2000
    assert summary["time_to_first_token_ms"]["observations"] == 3
    assert summary["time_to_first_token_ms"]["unmeasured"] == 1
    assert summary["turns"] == 4


def test_the_summary_of_nothing_is_unknown_rather_than_zero():
    from pressure_loop import turn_latency

    summary = turn_latency.summary()
    assert summary["turns"] == 0
    assert summary["time_to_first_token_ms"]["median"] is None
    assert summary["time_to_first_token_ms"]["observations"] == 0


def test_marking_is_not_a_gate_and_survives_a_broken_clock(monkeypatch):
    """Instrumentation must never be able to fail a turn."""
    from pressure_loop import turn_latency

    def _explode() -> float:
        raise RuntimeError("no clock")

    monkeypatch.setattr(turn_latency.time, "monotonic", _explode)
    assert turn_latency.mark("cyc-6", "arrival") is False
    assert turn_latency.snapshot("cyc-6") is None


def test_lane_and_pipeline_travel_with_the_turn_when_known():
    from pressure_loop import turn_latency

    turn_latency.mark("cyc-7", "arrival", now=0.0, work_kind="operator_turn")
    turn_latency.mark("cyc-7", "admitted", now=0.9, pipeline_id=2, lane="agent")

    snap = turn_latency.snapshot("cyc-7")
    assert snap["pipeline_id"] == 2
    assert snap["lane"] == "agent"
    assert snap["work_kind"] == "operator_turn"
    assert snap["lanes_busy_at_admission"] is None, "not measured yet is not zero"


# ── the marks come from the real paths, not from the test ────────────────────


def test_the_one_gate_marks_arrival_when_it_stamps_the_turn():
    """Arrival is the queue's own stamp, so the measurement starts where the law starts."""
    from pressure_loop import cognition_queue as cq
    from pressure_loop import turn_latency
    from pressure_loop.contributors import make_contributor

    cq._reset_cache_for_tests()
    turn_latency.reset_for_tests()
    contributor = make_contributor(
        "autonomous", "curiosity", 1.0,
        {"arrival_seq": 1, "cycle_id": "gate-1", "lane": "chat"}
    )
    assert cq.enqueue_input(contributor, operator_activity=False) is True

    snap = turn_latency.snapshot("gate-1")
    assert snap is not None, "the gate recorded no arrival for the turn it stamped"
    assert snap["since_arrival_ms"]["arrival"] == 0
    assert snap["work_kind"] == "curiosity"
    assert snap["time_to_reply_ms"] is None


def test_the_first_partial_of_the_answer_marks_first_token(monkeypatch):
    from pressure_loop import operator_chat_responses as ocr
    from pressure_loop import turn_latency

    monkeypatch.setattr("hud.chat_socket.publish_delta", lambda **kw: 1)
    turn_latency.mark("tok-1", "arrival", now=0.0)
    sink = ocr.reply_token_sink("tok-1", "ep-tok", pipeline_id=1)
    sink("Bis")
    sink("cuit")

    snap = turn_latency.snapshot("tok-1")
    assert snap["time_to_first_token_ms"] is not None
    assert snap["pipeline_id"] == 1
    assert snap["missing"] == ["admitted", "reply", "released"], (
        "this test marks no admission, and the instrument must say so rather than imply one"
    )


def test_the_durable_answer_marks_the_reply(monkeypatch):
    from pressure_loop import operator_chat_responses as ocr
    from pressure_loop import turn_latency

    monkeypatch.setattr(ocr, "_persist", lambda: None)
    monkeypatch.setattr("hud.chat_socket.publish_reply", lambda _record: 0)
    monkeypatch.setattr(ocr, "remember_assistant_turn", lambda *a, **k: None)
    turn_latency.mark("rep-1", "arrival", now=0.0)

    recorded = ocr.append_response(cycle_id="rep-1", episode_id="ep-rep",
                                   text="Biscuit.")
    assert recorded is not None
    snap = turn_latency.snapshot("rep-1")
    assert snap["time_to_reply_ms"] is not None
    assert snap["missing"] == ["admitted", "first_token", "released"], (
        "a reply with no admission mark must say so rather than imply a fast turn"
    )


def test_a_measured_turn_never_reports_a_reply_before_its_first_token():
    """The instrument's own honesty check: the phases cannot claim an impossible order."""
    from pressure_loop import turn_latency

    turn_latency.mark("ord-1", "arrival", now=0.0)
    turn_latency.mark("ord-1", "reply", now=4.0)
    turn_latency.mark("ord-1", "first_token", now=2.0)

    snap = turn_latency.snapshot("ord-1")
    assert snap["time_to_first_token_ms"] == 2000
    assert snap["time_to_reply_ms"] == 4000
    assert snap["time_to_first_token_ms"] <= snap["time_to_reply_ms"]


def test_admission_marks_the_queue_wait_and_the_load_it_was_admitted_under():
    """B2 is "under 2 s WHILE two lanes are leased", so the load is part of the measurement."""
    import cognition_chain

    assert cognition_chain._lanes_busy_at_admission(0) == 0, "no lane is in flight in this test"
    assert cognition_chain._lanes_busy_at_admission(None) is not None


def test_the_load_reads_as_unknown_rather_than_idle_when_it_cannot_be_read(monkeypatch):
    import cognition_chain
    from pressure_loop import pipeline_dispatch

    def _explode(_pid):
        raise RuntimeError("dispatch unavailable")

    monkeypatch.setattr(pipeline_dispatch, "pipeline_in_flight", _explode)
    assert cognition_chain._lanes_busy_at_admission(0) is None, (
        "an unreadable load must not present as an idle machine"
    )


def test_the_telemetry_read_carries_the_populations_and_bounds_its_limit():
    """The read is diagnostic: it reports what was measured, and says what was not."""
    import asyncio

    from hud.server import create_app
    from pressure_loop import turn_latency

    for index in range(3):
        cycle = f"tel-{index}"
        turn_latency.mark(cycle, "arrival", now=0.0)
        turn_latency.mark(cycle, "admitted", now=0.4)
        turn_latency.mark(cycle, "first_token", now=1.2 + index)

    endpoint = None
    for route in create_app().routes:
        if getattr(route, "path", "") == "/api/telemetry/turn-latency":
            endpoint = route.endpoint
    assert endpoint is not None, "no read surface for the latency marks"

    import json

    body = json.loads(asyncio.run(endpoint(limit=2)).body)
    assert len(body["turns"]) == 2, "the limit is honoured"
    assert body["summary"]["turns"] == 3
    assert body["summary"]["time_to_first_token_ms"]["observations"] == 3
    assert body["summary"]["time_to_reply_ms"]["median"] is None, "no turn replied"
    assert body["summary"]["time_to_reply_ms"]["unmeasured"] == 3


# ── the lane the turn occupied, and when it came back ────────────────────────


def test_the_phases_include_the_release_of_the_lane():
    from pressure_loop import turn_latency

    assert turn_latency.TURN_PHASES == (
        "arrival", "admitted", "first_token", "reply", "released",
    )


def test_a_turn_reports_how_long_it_held_a_lane_and_how_much_of_that_was_after_the_answer():
    """The capacity question: the operator had their answer at 4.7 s and the lane stayed busy
    until 22.9 s. That tail is what steals the pipeline the next sentence needs."""
    from pressure_loop import turn_latency

    turn_latency.mark("cap-1", "arrival", now=0.0)
    turn_latency.mark("cap-1", "admitted", now=0.8)
    turn_latency.mark("cap-1", "first_token", now=2.5)
    turn_latency.mark("cap-1", "reply", now=4.7)
    turn_latency.mark("cap-1", "released", now=22.9)

    snap = turn_latency.snapshot("cap-1")
    assert snap["capacity_held_ms"] == 22100
    assert snap["after_reply_ms"] == 18200, "the terminus tail is the measurement that matters"
    assert snap["complete"] is True


def test_a_lane_that_has_not_been_released_reports_unknown_not_finished():
    from pressure_loop import turn_latency

    turn_latency.mark("cap-2", "arrival", now=0.0)
    turn_latency.mark("cap-2", "admitted", now=0.5)
    turn_latency.mark("cap-2", "reply", now=3.0)

    snap = turn_latency.snapshot("cap-2")
    assert snap["capacity_held_ms"] is None, "a lane still held has no holding time yet"
    assert snap["after_reply_ms"] is None
    assert snap["missing"] == ["first_token", "released"]


def test_a_release_without_an_answer_still_reports_the_lane_it_held():
    """An episode that failed or answered nothing still occupied a pipeline, and that cost is
    real: it must not vanish because there was no reply to subtract from."""
    from pressure_loop import turn_latency

    turn_latency.mark("cap-3", "arrival", now=0.0)
    turn_latency.mark("cap-3", "admitted", now=0.4)
    turn_latency.mark("cap-3", "released", now=9.4)

    snap = turn_latency.snapshot("cap-3")
    assert snap["capacity_held_ms"] == 9000
    assert snap["after_reply_ms"] is None, "no answer means no tail to measure, not a zero tail"


def test_the_summary_carries_the_capacity_distributions():
    from pressure_loop import turn_latency

    for index, released in enumerate((10.0, 16.0, 28.0)):
        cycle = f"s-{index}"
        turn_latency.mark(cycle, "arrival", now=0.0)
        turn_latency.mark(cycle, "admitted", now=1.0)
        turn_latency.mark(cycle, "reply", now=5.0)
        turn_latency.mark(cycle, "released", now=released)

    summary = turn_latency.summary()
    assert summary["after_reply_ms"]["median"] == 11000
    assert summary["after_reply_ms"]["p95"] == 23000
    assert summary["capacity_held_ms"]["observations"] == 3


def test_the_dispatcher_marks_the_release_from_its_own_finally():
    """The mark belongs to the code that actually frees the pipeline, so a cascade that failed
    or raised still records the lane coming back."""
    import inspect

    from pressure_loop import pipeline_dispatch

    source = inspect.getsource(pipeline_dispatch.dispatch_cascade)
    release_at = source.index("_IN_FLIGHT.remove(pid)")
    mark_at = source.index('"released"')
    assert mark_at < release_at, "the release is marked where the pipeline is actually freed"
