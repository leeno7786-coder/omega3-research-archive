"""LongMemEval keeps its evaluation-only encoder separate from product runtime."""

from __future__ import annotations

import pytest


def test_longmemeval_does_not_fallback_to_retired_runtime_embedding(monkeypatch):
    from benchmarks.longmemeval import local_embed, run_eval

    monkeypatch.setattr(local_embed, "available", lambda: False)

    def _runtime_embedding_is_retired():
        raise AssertionError("LongMemEval must not use runtime embedding APIs")

    monkeypatch.setattr("runtime.client.get_runtime_client", _runtime_embedding_is_retired)

    with pytest.raises(RuntimeError, match="LongMemEval.*local embedding"):
        run_eval._build_semantic_store({"memory-id": ("", "", "text", "text")})
