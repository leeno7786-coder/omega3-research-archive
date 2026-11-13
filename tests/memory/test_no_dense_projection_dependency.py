"""Projection lifecycle uses the packed DME + Ettin retina, never dense Gemma."""

from __future__ import annotations

from pathlib import Path

import deepsleep_loop.loop as deepsleep_loop
import memory.api as memory_api
import memory.encoding as encoding
import memory.engine.reindex as reindex
import memory.gates.base as gate_base
import memory.promotion as promotion


def test_deepsleep_and_reindex_have_no_legacy_dense_embed_dependency():
    deepsleep_source = Path(deepsleep_loop.__file__).read_text(encoding="utf-8")
    reindex_source = Path(reindex.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "memory.embed_backfill",
        "memory.semantic_state_engine",
        "drain_pending_embeds",
        "assert_single_embedding_geometry",
    ):
        assert forbidden not in deepsleep_source
        assert forbidden not in reindex_source


def test_reindex_public_api_has_no_dense_drain_switch():
    import inspect

    assert tuple(inspect.signature(reindex.reindex_and_compile).parameters) == ("root",)


def test_semantic_admission_has_no_compiled_graph_dependency():
    import inspect

    from memory.semantic_admission import semantic_admission

    assert tuple(inspect.signature(semantic_admission).parameters) == ("records", "query", "k")


def test_active_memory_write_paths_have_no_legacy_dense_embed_dependency():
    import inspect

    active_sources = (
        inspect.getsource(encoding.encode_committed_memory),
        inspect.getsource(promotion.commit_promotion),
        inspect.getsource(promotion.commit_flagged),
        inspect.getsource(gate_base.MemoryGate._write),
        inspect.getsource(memory_api._observed_embedding_substrate),
        inspect.getsource(memory_api._probe_embedding_substrate_if_unknown),
    )
    for source in active_sources:
        for forbidden in (
            "memory.embed_backfill",
            "memory.local_embed",
            "memory.semantic_state_engine",
            "local_worker_reachable",
            "queue_pending_embed",
            "_attach_embedding",
            "_defer_embedding",
        ):
            assert forbidden not in source


def test_identity_encoding_is_native_and_never_calls_dense_embed(monkeypatch, tmp_path):
    import memory.queues as queues
    import memory.strength_index as strength_index

    class _Strength:
        def register(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(strength_index, "ensure_strength_index", lambda: _Strength())
    monkeypatch.setattr(queues, "enqueue_reflection_eligible", lambda *_args: True)
    monkeypatch.setattr(queues, "enqueue_deepsleep_candidate", lambda *_args: True)
    memory_dict = {
        "id": "0.99.ettin_native",
        "coord": [0, 99, 0],
        "type": "concept",
        "text": "Omega uses the resident Ettin retina.",
        "provenance": {"confidence": 0.8},
    }
    result = encoding.encode_committed_memory(memory_dict, str(tmp_path / "identity.json"))

    assert result.ok is True
    assert result.is_identity_write is True
    assert "native_projection_eligible" in result.steps_completed
    assert "embed" not in result.steps_completed
    assert "embeddings" not in memory_dict
