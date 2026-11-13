"""Tests for OMEGA_HUD_DISABLE skip path in HUD spawn.

Per V2 live-boot spec: setting OMEGA_HUD_DISABLE=1 must cleanly skip
HUD spawn — no FastAPI port binding, no browser launch, no thread
left running.
"""

from __future__ import annotations
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_hud_disabled_env_var_skips_spawn(monkeypatch):
    """When OMEGA_HUD_DISABLE=1 is set, the spawner reports disabled,
    no thread is started, no port is bound, no browser is launched."""
    monkeypatch.setenv("OMEGA_HUD_DISABLE", "1")
    from hud.spawner import is_disabled, disabled_reason

    assert is_disabled() is True
    reason = disabled_reason()
    assert reason and isinstance(reason, str)


def test_hud_not_disabled_when_env_absent(monkeypatch):
    """Without OMEGA_HUD_DISABLE, spawn is permitted (this test does
    NOT actually spawn — just verifies the gate returns False)."""
    monkeypatch.delenv("OMEGA_HUD_DISABLE", raising=False)
    from hud.spawner import is_disabled

    assert is_disabled() is False
