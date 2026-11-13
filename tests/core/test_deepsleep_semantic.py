"""Slice 6c B2 — DeepSleep semantic store + effect helpers (promotion in window consolidation tests)."""

from __future__ import annotations

from deepsleep_loop._deepsleep_state import SemanticEntry
from deepsleep_loop.semantic.run import semantic_effects_from_entries
from deepsleep_loop.semantic_store import load_semantic, upsert_semantic


def _semantic_entry(**kwargs) -> SemanticEntry:
    defaults = {
        "domain": "quantum_physics",
        "statement": "foundational understanding of quantum mechanics",
        "reinforcement_count": 1,
        "promoted_episode": "ep-1",
    }
    defaults.update(kwargs)
    return SemanticEntry(**defaults)


def test_semantic_entry_round_trip():
    original = _semantic_entry(reinforcement_count=3)
    restored = SemanticEntry.from_dict(original.to_dict())
    assert restored == original


def test_upsert_semantic_idempotent(tmp_path):
    path = tmp_path / "semantic.json"
    entry = _semantic_entry()
    upsert_semantic(entry, path=path)
    upsert_semantic(entry, path=path)

    loaded = load_semantic(path=path)
    assert len(loaded) == 1


def test_semantic_effects_from_entries():
    from deepsleep_loop.semantic_store import semantic_key

    entries = [_semantic_entry()]
    effects = semantic_effects_from_entries(entries)
    key = semantic_key("quantum_physics", "foundational understanding of quantum mechanics")
    assert key in effects
    assert effects[key]["domain"] == "quantum_physics"


def test_semantic_effects_multiple_statements_same_domain_do_not_collapse():
    # Cascade audit 2026-07-09: N grounded statements in one domain must each survive
    # into semantic_effects (keyed per-statement), not collapse to the last one.
    entries = [
        _semantic_entry(statement="Dijkstra finds single-source shortest paths"),
        _semantic_entry(statement="BFS finds shortest paths in unweighted graphs"),
        _semantic_entry(statement="A* uses an admissible heuristic"),
    ]
    effects = semantic_effects_from_entries(entries)
    assert len(effects) == 3
    assert {v["statement"] for v in effects.values()} == {
        "Dijkstra finds single-source shortest paths",
        "BFS finds shortest paths in unweighted graphs",
        "A* uses an admissible heuristic",
    }


def test_deepsleep_semantic_policy_loaders():
    from pressure_loop.policy import clear_policy_cache, deepsleep_semantic_path

    clear_policy_cache()
    assert str(deepsleep_semantic_path()).endswith("semantic.json")
