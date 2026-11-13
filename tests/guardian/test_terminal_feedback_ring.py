"""In-process audit ring tests for the slim terminal_feedback rebuild."""

from __future__ import annotations

import pytest

from guardian import terminal_feedback as tf


@pytest.fixture(autouse=True)
def _isolate_ring():
    tf._reset_recent_events_ring_for_tests()
    yield
    tf._reset_recent_events_ring_for_tests()


def test_emit_phase_appends_to_ring():
    tf.emit_phase("test_loop", "test_phase", status="enter", summary="x")
    events = tf.recent_audit_events()
    assert len(events) == 1
    assert events[0]["kind"] == "phase"
    assert events[0]["loop"] == "test_loop"
    assert events[0]["phase"] == "test_phase"
    assert events[0]["status"] == "enter"
    assert events[0]["ts"]


def test_emit_signal_and_memory_also_append():
    tf.emit_signal("pressure.curiosity_gap", 0.4, narrative="gap")
    tf.emit_memory("recall", coord=[2, 0, 0], status="ok", candidate_count=3)
    kinds = [e["kind"] for e in tf.recent_audit_events()]
    assert kinds == ["signal", "memory"]


def test_limit_caps_result():
    for i in range(5):
        tf.emit_phase("loop", f"p{i}", status="ok")
    assert len(tf.recent_audit_events(limit=2)) == 2
    assert [e["phase"] for e in tf.recent_audit_events(limit=2)] == ["p3", "p4"]


def test_recent_audit_events_filters_and_reverses():
    tf.emit_phase("loop", "p0", status="ok")
    tf.emit_data("memory", "write_memory", validation_status="committed")
    tf.emit_data("memory", "read_memory", status="ok")
    tf.emit_data("memory", "write_memory", validation_status="rejected")

    rows = tf.recent_audit_events(
        predicate=lambda e: e.get("phase") == "write_memory",
        newest_first=True,
    )

    assert [r["validation_status"] for r in rows] == ["rejected", "committed"]


def test_ring_starts_empty():
    assert tf.recent_audit_events() == []


def test_reset_clears_ring():
    tf.emit_phase("loop", "phase", status="ok")
    tf._reset_recent_events_ring_for_tests()
    assert tf.recent_audit_events() == []
