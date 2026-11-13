"""Bulk HUD snapshot must not flood capability_check phase emits."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from runtime.runtime_capability_broker import (
    RuntimeCapability,
    check_runtime_capability,
    runtime_capability_snapshot,
)


def _collect_events():
    events = []

    def _cb(ev):
        events.append(ev)

    return events, patch("guardian.terminal_feedback._publish_event", side_effect=_cb)


def _capability_check_phases(events):
    return [
        ev
        for ev in events
        if ev.get("kind") == "phase"
        and ev.get("loop") == "broker"
        and ev.get("phase") == "capability_check"
    ]


def _capability_evaluate_data(events):
    return [
        ev
        for ev in events
        if ev.get("kind") == "data"
        and ev.get("loop") == "runtime"
        and ev.get("phase") == "capability_evaluate"
    ]


def test_runtime_capability_snapshot_emits_zero_capability_check_phases():
    events, ctx = _collect_events()
    fake_client = MagicMock()
    fake_client.generate_completion = lambda **kw: None

    with ctx:
        runtime_capability_snapshot(
            client=fake_client,
        )

    assert _capability_check_phases(events) == []
    assert _capability_evaluate_data(events) == []


def test_runtime_capability_snapshot_probes_health_once_when_down():
    calls = {"n": 0}

    class _Client:
        def health_body(self):
            calls["n"] += 1
            return None

    runtime_capability_snapshot(client=_Client())
    assert calls["n"] == 1


def test_direct_capability_check_still_emits_one_pair():
    events, ctx = _collect_events()
    fake_client = MagicMock()
    fake_client.generate_completion = lambda **kw: None

    with ctx:
        check_runtime_capability(
            RuntimeCapability.CODING_MODEL,
            client=fake_client,
            health_body={"status": "ok", "capabilities": {"coding_model": True}},
        )

    phases = _capability_check_phases(events)
    assert len(phases) == 2
    assert phases[0]["status"] == "enter"
    assert phases[1]["status"] == "ok"
    assert phases[0].get("capability") == "coding_model"
