"""
Broker phase-observability tests.

Covers:
  - check_runtime_capability enter/exit + payload
  - preflight_runtime_operation enter/exit + payload
  - exception → fail emit
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from runtime.runtime_capability_broker import (
    check_runtime_capability,
    preflight_runtime_operation,
    RuntimeCapability,
)


def _collect_events():
    """Returns (events_list, patch_ctx)."""
    events = []

    def _cb(ev):
        events.append(ev)

    return events, patch("guardian.terminal_feedback._publish_event", side_effect=_cb)


def _phases(events, *, loop=None, phase=None, status=None):
    out = []
    for ev in events:
        if ev.get("kind") != "phase":
            continue
        if loop is not None and ev.get("loop") != loop:
            continue
        if phase is not None and ev.get("phase") != phase:
            continue
        if status is not None and ev.get("status") != status:
            continue
        out.append(ev)
    return out


# ──────────────────────────────────────────────────────────────────────
# check_runtime_capability
# ──────────────────────────────────────────────────────────────────────


def test_capability_check_available_emits_enter_exit():
    events, ctx = _collect_events()
    fake_client = MagicMock()
    fake_client.generate_completion = lambda **kw: None

    with ctx:
        result = check_runtime_capability(
            RuntimeCapability.VOICE,
            client=fake_client,
            health_body={"status": "ok", "capabilities": {"voice": True}},
        )

    assert result.state.value == "available"
    phases = _phases(events, loop="broker", phase="capability_check")
    assert len(phases) == 2
    assert phases[0]["status"] == "enter"
    assert phases[0].get("capability") == "voice"
    assert phases[1]["status"] == "ok"
    assert phases[1].get("state") == "available"
    assert phases[1].get("route") == "proceed"
    assert phases[1].get("available") is True


def test_capability_check_down_emits_exit_with_payload():
    events, ctx = _collect_events()
    fake_client = MagicMock()

    with ctx:
        result = check_runtime_capability(
            RuntimeCapability.VOICE,
            client=fake_client,
            health_body={"status": "unhealthy"},
        )

    assert result.state.value == "down"
    phases = _phases(events, loop="broker", phase="capability_check")
    assert len(phases) == 2
    assert phases[1]["status"] == "ok"  # span exits normally even for DOWN
    assert phases[1].get("state") == "down"
    assert phases[1].get("route") == "response_hold"
    assert phases[1].get("reason") == "runtime_unreachable_or_unhealthy"


def test_capability_check_degraded_missing_method():
    events, ctx = _collect_events()
    # Use a plain object that lacks the required client_method
    fake_client = object()

    with ctx:
        result = check_runtime_capability(
            RuntimeCapability.VOICE,
            client=fake_client,
            health_body={"status": "ok"},
        )

    assert result.state.value == "degraded"
    phases = _phases(events, loop="broker", phase="capability_check")
    assert phases[1]["status"] == "ok"
    assert phases[1].get("state") == "degraded"
    assert phases[1].get("reason") == "client_method_missing"


# ──────────────────────────────────────────────────────────────────────
# preflight_runtime_operation
# ──────────────────────────────────────────────────────────────────────


def test_preflight_emits_enter_exit_with_receipt():
    events, ctx = _collect_events()
    fake_client = MagicMock()
    fake_client.generate_completion = lambda **kw: None

    with ctx:
        decision = preflight_runtime_operation(
            RuntimeCapability.VOICE,
            operation="autonomous_generate",
            cycle_id="cyc-123",
            client=fake_client,
            health_body={"status": "ok", "capabilities": {"voice": True}},
        )

    assert decision.proceed is True
    phases = _phases(events, loop="broker", phase="autonomous_generate")
    assert len(phases) == 2
    assert phases[0]["status"] == "enter"
    assert phases[0].get("cycle_id") == "cyc-123"
    assert phases[1]["status"] == "ok"
    assert phases[1].get("proceed") is True
    assert phases[1].get("route") == "proceed"
    assert phases[1].get("state") == "available"


def test_preflight_degraded_emits_exit_degraded():
    events, ctx = _collect_events()
    fake_client = MagicMock()

    with ctx:
        decision = preflight_runtime_operation(
            RuntimeCapability.VOICE,
            operation="stt_transcribe",
            cycle_id="cyc-456",
            client=fake_client,
            health_body={"status": "unhealthy"},
        )

    assert decision.proceed is False
    phases = _phases(events, loop="broker", phase="stt_transcribe")
    assert len(phases) == 2
    assert phases[1]["status"] == "ok"
    assert phases[1].get("proceed") is False
    assert phases[1].get("state") == "down"
    assert phases[1].get("route") == "response_hold"
