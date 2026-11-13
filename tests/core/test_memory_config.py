"""Memory subsystem reads governed config bundle."""

from __future__ import annotations

from config import loader
from core import memory_config as mc


def test_memory_enabled_from_bundle():
    loader.clear_cache()
    mc.reset_memory_config_for_tests()
    assert mc.memory_enabled() is True


def test_memory_enabled_respects_config(monkeypatch):
    loader.clear_cache()
    mc.reset_memory_config_for_tests()
    monkeypatch.setattr(
        "config.loader.load_subsystem_bundle",
        lambda _stem: {"config": {"memory": {"enabled": False}}, "policy": {}},
    )
    assert mc.memory_enabled() is False
