"""Telemetry snapshot reads the audit ring once per build."""

from __future__ import annotations

from unittest.mock import patch

from hud import telemetry


def test_telemetry_snapshot_captures_audit_ring_once(monkeypatch):
    telemetry._SNAPSHOT_CACHE = None
    telemetry._SNAPSHOT_CACHE_TS = 0.0
    telemetry._ACTIVE_AUDIT_SNAPSHOT = None
    calls = {"n": 0}
    sample = [{"kind": "phase", "loop": "pressure", "phase": "tick", "ts": "2026-01-01T00:00:00Z"}]

    def _fake_recent(limit=100, *, predicate=None, newest_first=False):
        calls["n"] += 1
        rows = list(sample)
        if predicate is not None:
            rows = [r for r in rows if predicate(r)]
        if newest_first:
            rows = list(reversed(rows))
        if limit <= 0:
            return rows
        if newest_first:
            return rows[:limit]
        return rows[-limit:]

    monkeypatch.setattr(
        "guardian.terminal_feedback.recent_audit_events",
        _fake_recent,
    )
    monkeypatch.setattr(telemetry, "_telemetry_cycle", lambda: {"id": "x", "live": False})
    monkeypatch.setattr(telemetry, "_cadence_tick_report", lambda: None)
    monkeypatch.setattr(telemetry, "_telemetry_memory", lambda: {})
    monkeypatch.setattr(telemetry, "_telemetry_omega", lambda: {})
    monkeypatch.setattr(telemetry, "_telemetry_meta", lambda: {})
    monkeypatch.setattr(telemetry, "_telemetry_global", lambda: {})
    monkeypatch.setattr(telemetry, "_telemetry_autoloop", lambda **_: {})
    monkeypatch.setattr(telemetry, "_telemetry_identity", lambda: {})
    monkeypatch.setattr(telemetry, "_telemetry_system", lambda: {})
    monkeypatch.setattr(telemetry, "_telemetry_layers", lambda _n: [])
    monkeypatch.setattr(telemetry, "_telemetry_brain", lambda **_: {})

    try:
        telemetry.telemetry_snapshot()
        assert calls["n"] == 1
    finally:
        telemetry._SNAPSHOT_CACHE = None
        telemetry._SNAPSHOT_CACHE_TS = 0.0
        telemetry._ACTIVE_AUDIT_SNAPSHOT = None
