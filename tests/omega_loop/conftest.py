"""Suite conftest — hermetic memory writes (2026-07-11 pollution incident).

Tests in this suite can reach real memory write paths (cascade folds, episode
cognition, memory.api). Without this redirect they wrote test junk into the
canonical brain. See tests/helpers/memory_isolation.py.
"""

from __future__ import annotations

import pytest

from tests.helpers.memory_isolation import redirect_memory_to_tmp


@pytest.fixture(autouse=True)
def _isolate_memory_root(tmp_path, monkeypatch):
    redirect_memory_to_tmp(tmp_path, monkeypatch)
    yield


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
