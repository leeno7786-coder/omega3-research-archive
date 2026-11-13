"""Recall routing failures must remain distinguishable from a valid route."""

from __future__ import annotations

import pytest

from auto_loop.mril_tools import router
from memory.engine import walker


def test_grammar_router_failure_is_not_recast_as_knowledge(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken_route(_query: str) -> dict:
        raise RuntimeError("grammar router unavailable")

    monkeypatch.setattr(router, "route_memory_tool", _broken_route)

    with pytest.raises(RuntimeError, match="grammar router unavailable"):
        walker._grammar_seed("where is the remembered fact")
