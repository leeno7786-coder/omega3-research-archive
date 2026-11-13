"""Slice 6d B1 — DeepSleep open query store + effect helpers (lifecycle in window consolidation tests)."""

from __future__ import annotations

from deepsleep_loop._deepsleep_state import OpenQuery
from tests.core.deepsleep_window_fixtures import query_input as _query_input
from deepsleep_loop.query_lifecycle.run import query_effects_from_queries
from deepsleep_loop.query_store import upsert_query
from deepsleep_loop.reseed.run import generate_reseed


def test_open_query_round_trip():
    original = OpenQuery(
        query_id="q-1",
        question="What is entanglement?",
        domain="quantum_physics",
        created_episode="ep-1",
        confidence=0.7,
        status="open",
        created_timestamp="2026-06-05T12:00:00Z",
    )
    restored = OpenQuery.from_dict(original.to_dict())
    assert restored == original


def test_archived_query_emits_no_reseed(tmp_path, monkeypatch):
    path = tmp_path / "open_queries.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_query_path",
        lambda policy=None: path,
    )

    archived = OpenQuery(
        query_id="q-archived",
        question="Old question",
        domain="quantum_physics",
        created_episode="ep-old",
        confidence=0.9,
        status="archived",
        created_timestamp="2026-06-05T12:00:00Z",
    )
    upsert_query(archived, path=path)

    contributors = generate_reseed(_query_input("ep-2"))
    assert [c for c in contributors if c.type == "outstanding_question"] == []


def test_query_effects_from_queries_reports_deltas():
    prior = {
        "q-1": OpenQuery(
            query_id="q-1",
            question="Q?",
            domain="physics",
            created_episode="ep-1",
            confidence=0.5,
            status="open",
            created_timestamp="t0",
        )
    }
    updated = [
        OpenQuery(
            query_id="q-1",
            question="Q?",
            domain="physics",
            created_episode="ep-1",
            confidence=0.5,
            status="archived",
            created_timestamp="t0",
            resolution_episode="ep-2",
            resolution_reason="done",
        ),
        OpenQuery(
            query_id="q-2",
            question="New?",
            domain="physics",
            created_episode="ep-2",
            confidence=0.4,
            status="open",
            created_timestamp="t1",
        ),
    ]
    effects = query_effects_from_queries(updated, prior=prior)
    created = [e for e in effects if e.get("query_id") == "q-2"]
    assert len(created) == 1
    assert created[0]["status"] == "open"
