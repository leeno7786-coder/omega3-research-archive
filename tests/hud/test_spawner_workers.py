"""HUD spawner must stay single-process for cognition_lock safety."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_spawn_hud_configures_uvicorn_with_single_worker(monkeypatch):
    import hud.spawner as spawner

    captured: dict = {}

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = True

        def run(self):
            return None

    class FakeConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self._target = kwargs.get("target")

        def start(self):
            return None

        def is_alive(self):
            return True

    monkeypatch.setattr(spawner, "_thread", None)
    monkeypatch.setattr(spawner, "_server", None)
    monkeypatch.setattr(spawner, "_disabled_reason", None)
    monkeypatch.delenv("OMEGA_HUD_DISABLE", raising=False)
    monkeypatch.setattr("uvicorn.Config", FakeConfig)
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    monkeypatch.setattr(spawner.threading, "Thread", FakeThread)

    url = spawner.spawn_hud_in_thread()
    assert url is not None
    assert captured.get("workers") == 1
