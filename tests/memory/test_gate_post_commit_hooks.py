from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_gate_memory(tmp_path, monkeypatch):
    import core.paths as paths
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.gates.base as gate_base
    import memory.strength_index as strength_index

    memory_root = tmp_path / "memory"
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{}", encoding="utf-8")
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(paths, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    monkeypatch.setattr(
        strength_index,
        "STRENGTH_INDEX_PATH",
        tmp_path / "guardian" / "state" / "strength_index.json",
    )
    monkeypatch.setattr(strength_index, "_singleton", None)
    monkeypatch.setattr(gate_base.MemoryGate, "_strength_index", None)

    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    walker._coord_index_generation = 0
    return tmp_path


class _GateWriteHarness:
    def __init__(self, source_tag: str):
        from memory.gates.base import MemoryGate

        class _HarnessGate(MemoryGate):
            READABLE_LOBES = []
            WRITABLE_LOBES = []
            DECOMPOSERS = {}
            SOURCE_TAG = source_tag

        self.gate = _HarnessGate()

    def write(
        self, *, coord: list[int], title: str, text: str, domain: str = "bob", enrichment=None
    ) -> str:
        ids: list[str] = []
        path = self.gate._write(
            text,
            coord,
            dict(enrichment or {}),
            {
                "title": title,
                "summary": text,
                "domain": domain,
                "subtopic": "gate_post_commit_hooks",
                "type": "concept",
                "confidence": 0.95,
            },
            id_out=ids,
        )
        assert path
        assert ids
        return ids[0]


def _name_enrichment(name: str) -> dict:
    return {
        "entities": [{"name": name, "facet": "name", "slot": "who", "rule": "test"}],
        "linked_persons": [name],
    }


def _record(memory_id: str) -> dict:
    from memory.engine import walker

    walker.load_coord_index()
    raw = (walker.COORD_INDEX.get(memory_id) or {}).get("path")
    assert raw, f"missing coord_index path for {memory_id}"
    # coord_index entries are profile-relative by design; resolve like recall does.
    path = walker.resolve_coord_index_path(raw)
    assert path, f"coord_index path did not resolve for {memory_id}: {raw!r}"
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_gate_atomic_writes_run_entity_linker_between_persona_and_situational(isolated_gate_memory):
    persona_gate = _GateWriteHarness("lobe:personas")
    situational_gate = _GateWriteHarness("lobe:situational")

    persona_id = persona_gate.write(
        coord=[1, 1, 1],
        title="Bob relationship",
        text="Bob is my dad.",
        enrichment=_name_enrichment("Bob"),
    )
    situational_id = situational_gate.write(
        coord=[4, 0, 1],
        title="Bob likes chicken",
        text="Bob likes chicken.",
        enrichment=_name_enrichment("Bob"),
    )

    persona = _record(persona_id)
    situational = _record(situational_id)

    assert situational_id in set(persona.get("links") or [])
    assert persona_id in set(situational.get("links") or [])


def test_gate_atomic_writes_run_supersedes_chain_hooks(isolated_gate_memory):
    persona_gate = _GateWriteHarness("lobe:personas")

    prior_id = persona_gate.write(
        coord=[1, 1, 1],
        title="Bob favorite food",
        text="Bob favorite food is chicken.",
        enrichment=_name_enrichment("Bob"),
    )
    new_id = persona_gate.write(
        coord=[1, 1, 2],
        title="Bob favorite food",
        text="Bob favorite food is fish.",
        enrichment=_name_enrichment("Bob"),
    )

    prior = _record(prior_id)
    new = _record(new_id)

    assert new.get("supersedes") == prior_id
    assert new.get("version") == 2
    assert prior.get("superseded_by") == new_id


def test_entity_link_peers_are_projection_work_not_a_full_rebuild(isolated_gate_memory, monkeypatch):
    """Live 2026-09-05: every persona relationship write logged
    'PROJECTION DIRTY — rebuild required: entity-link hook mutated N peer
    canonical file(s) not represented as projection work' and forced a full
    projection rebuild. The linker now enumerates each peer it rewrote, so the
    write owner emits the peer's own final delta and the projection stays
    clean."""
    import hashlib

    import memory.projection_delta as pd
    from memory.engine import walker
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    persona_gate = _GateWriteHarness("lobe:personas")
    situational_gate = _GateWriteHarness("lobe:situational")

    persona_id = persona_gate.write(
        coord=[1, 1, 1],
        title="Bob relationship",
        text="Bob is my dad.",
        enrichment=_name_enrichment("Bob"),
    )
    walker.load_coord_index()
    persona_path = Path(
        walker.resolve_coord_index_path((walker.COORD_INDEX.get(persona_id) or {}).get("path"))
    )
    # the write owner derives the projection root from the canonical path; read it the same way
    root = memory_root_for_canonical_path(persona_path)
    assert pd.projection_dirty_state(root=root)["dirty"] is False
    situational_id = situational_gate.write(
        coord=[4, 0, 1],
        title="Bob likes chicken",
        text="Bob likes chicken.",
        enrichment=_name_enrichment("Bob"),
    )

    persona = _record(persona_id)
    assert situational_id in set(persona.get("links") or [])  # the peer really was rewritten

    # no rebuild-required marker ...
    assert pd.projection_dirty_state(root=root)["dirty"] is False
    # ... because the peer's mutation is its own final delta, hashing the rewritten file
    deltas, malformed = pd.pending_projection_deltas(root=root)
    assert malformed == ()
    peer_deltas = [d for d in deltas if d.memory_id == persona_id]
    assert len(peer_deltas) >= 2, [(d.memory_id, d.operation, d.canonical_hash[:8]) for d in deltas]
    latest = peer_deltas[-1]
    assert latest.operation == "upsert"
    assert latest.canonical_hash == hashlib.sha256(persona_path.read_bytes()).hexdigest()
