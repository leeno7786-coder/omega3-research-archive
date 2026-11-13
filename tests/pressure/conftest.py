"""Pressure-suite isolation for operational lifecycle state."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_proactive_lifecycle(tmp_path, monkeypatch):
    import pressure_loop.proactive_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "_ledger_path",
        lambda: tmp_path / "proactive_lifecycle.jsonl",
    )
    lifecycle._reset_cache_for_tests()
    yield
    lifecycle._reset_cache_for_tests()
