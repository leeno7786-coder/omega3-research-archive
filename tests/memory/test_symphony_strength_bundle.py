"""Symphony strength snapshot honors governed bundles."""

from __future__ import annotations

from config import loader
from core import memory_config as mc
from core import strength_index_config as sic
from memory import symphony_strength


def test_strength_index_recency_windows_from_bundle():
    loader.clear_cache()
    sic.reset_strength_index_config_for_tests()
    windows = sic.aggregate_recency_seconds()
    live = loader.load_subsystem_bundle("strength_index")["config"]["strength_index"]["recency"]
    assert windows["hot_age_seconds"] == live["hot_age_hours"] * 3600.0


def test_symphony_strength_returns_unavailable_when_memory_disabled(monkeypatch):
    loader.clear_cache()
    mc.reset_memory_config_for_tests()
    monkeypatch.setattr(mc, "memory_enabled", lambda: False)
    snap = symphony_strength.build_strength_index_snapshot()
    assert snap["source"] == symphony_strength.SOURCE_UNAVAILABLE
