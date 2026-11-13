from __future__ import annotations

import json

from memory.gates.base import MemoryGate
from memory.sense_registry import (
    POLYSEMOUS_TERMS,
    register_senses_for_memory,
    resolve_query_senses,
)


def test_register_senses_for_memory_creates_distinct_polysemes(tmp_path):
    terms = {
        "neuron": (
            "Neuron",
            "A biological cell passes signals through synapses.",
            [2, 0, 1],
            "biology",
        ),
        "node": ("Node", "A graph vertex connects to other vertices.", [2, 0, 2], "graph theory"),
        "model": (
            "Model",
            "A machine learning model maps inputs to predictions.",
            [8, 0, 3],
            "machine learning",
        ),
        "agent": (
            "Agent",
            "A robot agent plans and acts in a physical environment.",
            [8, 0, 4],
            "robotics",
        ),
        "graph": (
            "Graph",
            "A memory graph relates canonical leaves by edges.",
            [2, 0, 5],
            "memory",
        ),
    }

    created = {}
    for term, (title, summary, coord, domain) in terms.items():
        sense_ids = register_senses_for_memory(
            memory_id=f"mem.{term}",
            title=title,
            summary=summary,
            text=summary,
            coord=coord,
            domain=domain,
            root=tmp_path,
        )
        assert len(sense_ids) == 1
        created[term] = sense_ids[0]

    assert set(terms) <= POLYSEMOUS_TERMS
    path = tmp_path / "memory" / "sense_registry" / "senses.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {row["term"] for row in rows} == set(terms)
    assert len({row["sense_id"] for row in rows}) == len(terms)

    assert resolve_query_senses("robot agent", root=tmp_path)[0]["sense_id"] == created["agent"]
    assert (
        resolve_query_senses("machine learning model", root=tmp_path)[0]["sense_id"]
        == created["model"]
    )


def test_memory_gate_write_stamps_sense_ids(monkeypatch, tmp_path):
    from memory.engine import validate_write

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")

    captured = {}

    class DummyGate(MemoryGate):
        WRITABLE_LOBES = [2]
        SOURCE_TAG = "gate:test"

    def fake_atomic_write(mem):
        captured["mem"] = mem
        return str(tmp_path / "memory" / "dummy.json")

    monkeypatch.setattr("memory.gates.base.atomic_write", fake_atomic_write)
    monkeypatch.setattr(MemoryGate, "_get_strength_index", classmethod(lambda cls: None))

    gate = DummyGate()
    path = gate._write(
        "A robot agent plans and acts in a physical environment.",
        [2, 0, 1],
        None,
        {"title": "Agent", "summary": "A robot agent plans.", "domain": "robotics"},
    )

    assert path
    sense_ids = captured["mem"]["enrichment"]["sense_ids"]
    assert len(sense_ids) == 1
    assert sense_ids[0].startswith("sense:agent:")
