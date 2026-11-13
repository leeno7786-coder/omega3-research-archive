"""Operator idle-learning toggle (left cockpit pill, 2026-07-18).

Pins: default ON (missing file), durable round-trip, governor gated by the
toggle, env kill-switch still wins, corrupt file fails OPEN (autonomy is
the resting posture — day-zero wipes the file, so a fresh boot is ON).
"""

from __future__ import annotations

from pressure_loop import idle_wake as iw


def test_toggle_roundtrip_governor_and_failure_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(iw, "_toggle_path", lambda: tmp_path / "idle_learning.json")
    monkeypatch.setenv("OMEGA_IDLE_WAKE_ENABLED", "1")

    assert iw.operator_idle_learning_enabled() is False, "fresh install starts paused"
    assert iw.governor_enabled() is False

    assert iw.set_operator_idle_learning(False) == {"enabled": False}
    assert iw.operator_idle_learning_enabled() is False
    assert iw.governor_enabled() is False, "operator toggle gates the governor"

    assert iw.set_operator_idle_learning(True) == {"enabled": True}
    assert iw.governor_enabled() is True

    monkeypatch.setenv("OMEGA_IDLE_WAKE_ENABLED", "0")
    assert iw.governor_enabled() is False, "env kill-switch still wins"

    monkeypatch.setenv("OMEGA_IDLE_WAKE_ENABLED", "1")
    (tmp_path / "idle_learning.json").write_text("{corrupt", encoding="utf-8")
    assert iw.operator_idle_learning_enabled() is False, "corrupt state fails safely paused"
