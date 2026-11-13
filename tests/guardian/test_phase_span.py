"""phase_span — paired enter/exit phase instrumentation helper."""

import pytest
from guardian import terminal_feedback as tf


def _capture(monkeypatch):
    events = []
    monkeypatch.setattr(tf, "_publish_event", lambda e: events.append(e))
    return events


def test_phase_span_enter_and_ok_exit(monkeypatch):
    events = _capture(monkeypatch)
    with tf.phase_span("omega", "p", cycle_id="c1", keys=19) as span:
        span.set(health="degradation", patterns=0)
    enter = [e for e in events if e["status"] == "enter"][0]
    assert enter["loop"] == "omega" and enter["phase"] == "p"
    assert enter["keys"] == 19 and enter["cycle_id"] == "c1"
    exit_ev = [e for e in events if e["status"] == "ok"][0]
    assert exit_ev["health"] == "degradation" and exit_ev["patterns"] == 0
    assert exit_ev["cycle_id"] == "c1"


def test_phase_span_skip_status(monkeypatch):
    events = _capture(monkeypatch)
    with tf.phase_span("auto", "p") as span:
        span.skip("nothing to do")
    assert any(e["status"] == "skip" and e["summary"] == "nothing to do" for e in events)


def test_phase_span_fail_on_exception_reraises(monkeypatch):
    events = _capture(monkeypatch)
    with pytest.raises(ValueError):
        with tf.phase_span("brain", "p", cycle_id="c2") as span:
            span.set(stage="mid")
            raise ValueError("boom")
    fail = [e for e in events if e["status"] == "fail"][0]
    assert fail["error_type"] == "ValueError" and "boom" in fail["summary"]
    assert fail["stage"] == "mid" and fail["cycle_id"] == "c2"
    # no normal "ok" exit was emitted on the exception path
    assert not any(e["status"] == "ok" for e in events)


def test_phase_span_emit_failure_is_soft(monkeypatch):
    # a feedback-bus error must NEVER break the wrapped work
    def boom(*a, **k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(tf, "emit_phase", boom)
    ran = []
    with tf.phase_span("omega", "p") as span:
        ran.append(1)
        span.set(x=1)
    assert ran == [1]
