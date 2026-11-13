"""Memory-suite conftest — paths-only hermetic redirect (2026-07-11 pollution incident).

Several tests here drive build_memory_dict/atomic_write directly and were writing
REAL memories (physics/quantum "knowledge", source-ledger artifacts) into the
canonical brain. This redirect points every write path at tmp — but unlike
tests/core's fixture it does NOT stub encode_committed_memory: this suite
white-box-tests the encoding pipeline itself, and tests that need embeds mock
them individually. The source ledger resolves its root through
validate_write.MEMORY_ROOT, so the same redirect covers it.
"""

from __future__ import annotations

import pytest

from tests.helpers.memory_isolation import redirect_memory_to_tmp


@pytest.fixture(autouse=True)
def _isolate_memory_paths(tmp_path, monkeypatch):
    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)
    yield
