"""Fetched-knowledge ingestion — world vs coding routing + receipts-or-fail.

Operator pipeline 2026-07-11: fetched material -> naming/comprehension inference
-> memory commit RIGHT AFTER inference -> fold. World knowledge lands in lobe 2 and
concrete coding knowledge in lobe 8. Persisted fields are replayed through one
deterministic classifier; a model kind hint cannot override that contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory import fetched_ingest
from memory import episode_sram


def _receipt(status: str, path=None):
    return SimpleNamespace(validation_status=status, path=path, message="")


@pytest.fixture(autouse=True)
def _episode_bucket():
    episode_sram.reset()
    episode_sram.open_bucket("test-fetched-ingest", pipeline_id=0)
    try:
        yield
    finally:
        episode_sram.reset()


def test_world_knowledge_routes_to_lobe_2(monkeypatch) -> None:
    envelopes = []
    monkeypatch.setattr(
        "memory.api.write_memory",
        lambda env, **_k: envelopes.append(env) or _receipt("committed", "p/1"),
    )
    monkeypatch.setattr(
        fetched_ingest, "resolve_domain_and_coord", lambda lobe, d: (d, [lobe, 7, 0])
    )

    receipt = fetched_ingest.ingest_fetched_knowledge(
        "ep-w",
        learned="photosynthesis converts light into chemical energy in chloroplasts",
        sources=[{"name": "Wikipedia — Photosynthesis", "url": "https://x/photo"}],
        domain="biology",
        kind_hint="world",
    )
    episode_sram.close_bucket("test-fetched-ingest")

    assert receipt["lobe"] == 2
    assert receipt["kind"] == "world"
    assert envelopes[0].coord == [2, 7, 0]
    assert envelopes[0].provenance == ["https://x/photo"]


def test_coding_knowledge_routes_to_lobe_8(monkeypatch) -> None:
    envelopes = []
    monkeypatch.setattr(
        "memory.api.write_memory",
        lambda env, **_k: envelopes.append(env) or _receipt("committed", "p/2"),
    )
    monkeypatch.setattr(
        fetched_ingest, "resolve_domain_and_coord", lambda lobe, d: (d, [lobe, 3, 0])
    )

    receipt = fetched_ingest.ingest_fetched_knowledge(
        "ep-c",
        learned=(
            "python decorators wrap a function object; the class method resolves via "
            "the module import path and unit tests assert the API contract"
        ),
        sources=[{"name": "Python docs — decorators", "url": "https://x/deco"}],
        domain="python-decorators",
        kind_hint="coding",
    )
    episode_sram.close_bucket("test-fetched-ingest")

    assert receipt["lobe"] == 8
    assert receipt["kind"] == "coding"
    assert envelopes[0].coord == [8, 3, 0]


def test_kind_hint_cannot_override_silent_deterministic_route(monkeypatch) -> None:
    monkeypatch.setattr("memory.api.write_memory", lambda env, **_k: _receipt("committed", "p/3"))
    monkeypatch.setattr(
        fetched_ingest, "resolve_domain_and_coord", lambda lobe, d: (d, [lobe, 0, 0])
    )
    coding = fetched_ingest.ingest_fetched_knowledge(
        "ep-t", learned="terse notes", sources=[], domain="misc", kind_hint="coding"
    )
    world = fetched_ingest.ingest_fetched_knowledge(
        "ep-t2", learned="terse notes", sources=[], domain="misc", kind_hint="world"
    )

    assert coding["lobe"] == 2
    assert world["lobe"] == 2


def test_kind_hint_cannot_override_generic_process_route(monkeypatch) -> None:
    """A generic process is knowledge unless its persisted fields name code."""
    monkeypatch.setattr("memory.api.write_memory", lambda env, **_k: _receipt("committed", "p/4"))
    monkeypatch.setattr(
        fetched_ingest, "resolve_domain_and_coord", lambda lobe, d: (d, [lobe, 0, 0])
    )
    receipt = fetched_ingest.ingest_fetched_knowledge(
        "ep-process",
        learned="first install dependencies, then configure and deploy",
        sources=[],
        domain="deployment workflow",
        kind_hint="coding",
    )
    episode_sram.close_bucket("test-fetched-ingest")

    assert receipt["lobe"] == 2


def test_persisted_record_replays_to_the_ingested_lobe(monkeypatch) -> None:
    envelopes = []
    monkeypatch.setattr(
        "memory.api.write_memory",
        lambda env, **_k: envelopes.append(env) or _receipt("committed", "p/replay"),
    )
    monkeypatch.setattr(
        fetched_ingest, "resolve_domain_and_coord", lambda lobe, d: (d, [lobe, 0, 0])
    )

    receipt = fetched_ingest.ingest_fetched_knowledge(
        "ep-replay",
        learned="first install dependencies, then configure and deploy",
        sources=[],
        domain="deployment workflow",
        kind_hint="coding",
    )
    episode_sram.close_bucket("test-fetched-ingest")

    from memory.routing.coding_vs_knowledge import classify_memory_record_lobe

    assert classify_memory_record_lobe(envelopes[0].payload) == receipt["lobe"]


def test_failed_commit_fails_hard(monkeypatch) -> None:
    monkeypatch.setattr("memory.api.intake_memory", lambda env, **_k: _receipt("rejected"))
    monkeypatch.setattr(
        fetched_ingest, "resolve_domain_and_coord", lambda lobe, d: (d, [lobe, 0, 0])
    )

    with pytest.raises(RuntimeError, match="memory is first and truth"):
        fetched_ingest.ingest_fetched_knowledge(
            "ep-f", learned="something", sources=[], domain="x", kind_hint="world"
        )


def test_empty_prose_is_refused() -> None:
    with pytest.raises(ValueError):
        fetched_ingest.ingest_fetched_knowledge("ep-e", learned="  ", sources=[], domain="x")
