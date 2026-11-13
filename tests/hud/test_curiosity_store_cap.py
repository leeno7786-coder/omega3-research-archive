"""Curiosity parallel-pursuit cap (2026-07-04 reorg, Phase 3).

Before this wiring, curiosity.yaml was DEAD config: curiosity_store's
docstrings CLAIMED count_open() enforced limits.max_parallel_curiosity_threads
but nothing read the yaml and nothing consulted the counter. persist() now
enforces the cap at the canonical write path for NEW live entries.

Both directions, per the RED doctrine: the cap VALUE comes from the config
(assert == 3, not just "a limit exists"), a new entry at the cap RAISES, and
a rewrite of an existing entry at the cap PASSES (else nothing at the cap
could ever be resolved).
"""

from __future__ import annotations

from dataclasses import replace
from typing import get_args

import pytest

from schema.curiosity_engine import (
    CuriosityEngineEntry,
    GapType,
    ResolutionPath,
    TriggerSource,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from hud import curiosity_store

    monkeypatch.setattr(curiosity_store, "_STORE_DIR", tmp_path / "open_questions")
    return curiosity_store


def _entry(n: int, status: str = "open") -> CuriosityEngineEntry:
    return CuriosityEngineEntry(
        cycle_id=f"cycle-{n}",
        curiosity_id=f"cap-test-{n}",
        created_at="2026-07-04T00:00:00+00:00",
        trigger_source=get_args(TriggerSource)[0],
        gap_type=get_args(GapType)[0],
        problem_statement=f"cap test entry {n}",
        tension_score=0.5,
        resolution_value=0.5,
        urgency_score=0.5,
        persistence_score=0.5,
        resource_cost_estimate=0.1,
        recommended_path=get_args(ResolutionPath)[0],
        should_pursue=True,
        status=status,
    )


def test_cap_value_comes_from_config(store):
    from config.loader import load_curiosity_config

    configured = load_curiosity_config()["limits"]["max_parallel_curiosity_threads"]
    assert store._max_parallel_threads() == configured == 3


def test_new_entry_at_cap_raises(store):
    cap = store._max_parallel_threads()
    for i in range(cap):
        store.persist(_entry(i))
    assert store.count_open() == cap
    with pytest.raises(ValueError, match="max_parallel_curiosity_threads"):
        store.persist(_entry(cap))
    assert store.count_open() == cap


def test_rewrite_of_existing_entry_at_cap_passes(store):
    cap = store._max_parallel_threads()
    entries = [_entry(i) for i in range(cap)]
    for e in entries:
        store.persist(e)
    # status rewrite of an already-stored id must not be blocked at the cap —
    # this is exactly how an open entry gets resolved.
    store.persist(replace(entries[0], status="active"))
    resolved = store.mark_resolved(entries[1].curiosity_id, resolution_summary="closed by cap test")
    assert resolved.status == "resolved"
    # ...and the freed slot admits a new entry again.
    store.persist(_entry(cap + 1))


def test_below_cap_new_entries_pass(store):
    store.persist(_entry(0))
    store.persist(_entry(1))
    assert store.count_open() == 2
