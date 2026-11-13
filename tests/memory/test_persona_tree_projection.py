from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    import core.paths as paths
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker

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
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    walker._coord_index_generation = 0
    return tmp_path


def _name_enrichment(name: str) -> dict:
    return {
        "entities": [{"name": name, "facet": "name", "slot": "who", "rule": "test"}],
        "linked_persons": [name],
    }


def _write_record(
    *,
    text: str,
    coord: list[int],
    title: str,
    domain: str,
    enrichment: dict,
    links=None,
    supersedes=None,
    superseded_by=None,
    version: int = 1,
) -> str:
    from memory.core import build_memory_dict
    from memory.engine.validate_write import atomic_write

    metadata = {
        "title": title,
        "summary": text,
        "domain": domain,
        "subtopic": "persona_tree_test",
        "type": "concept",
    }
    if links:
        metadata["links"] = list(links)
    record = build_memory_dict(
        text, coord, "test:persona_tree", enrichment=enrichment, metadata=metadata
    )
    if supersedes is not None:
        record["supersedes"] = supersedes
    if superseded_by is not None:
        record["superseded_by"] = superseded_by
    if version:
        record["version"] = version
    atomic_write(record, update_index=True)
    return str(record["id"])


def _patch_record(memory_id: str, **updates) -> None:
    from memory.engine import walker

    walker.load_coord_index()
    # coord_index entries are profile-relative by design; resolve like recall does.
    path = walker.resolve_coord_index_path(walker.COORD_INDEX[memory_id]["path"])
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def test_persona_tree_groups_current_facts_history_relationships_and_events(isolated_memory):
    from memory.persona_tree import build_persona_tree

    prior_id = _write_record(
        text="Bob favorite food is chicken.",
        coord=[1, 1, 1],
        title="Bob favorite food",
        domain="bob",
        enrichment=_name_enrichment("Bob"),
    )
    new_id = _write_record(
        text="Bob favorite food is fish.",
        coord=[1, 1, 2],
        title="Bob favorite food",
        domain="bob",
        enrichment=_name_enrichment("Bob"),
        supersedes=prior_id,
        version=2,
    )
    _patch_record(prior_id, superseded_by=new_id)
    event_id = _write_record(
        text="Bob likes chess.",
        coord=[4, 0, 1],
        title="Bob likes chess",
        domain="system",
        enrichment=_name_enrichment("Bob"),
    )
    relationship_id = _write_record(
        text="Bob is the operator's mentor.",
        coord=[1, 1, 3],
        title="Bob mentor relationship",
        domain="bob",
        enrichment=_name_enrichment("Bob"),
        links=[
            {
                "kind": "persona_relationship",
                "relation": "mentor",
                "other_id": "operator",
                "other_name": "Operator",
            }
        ],
    )
    _write_record(
        text="Alice likes hiking.",
        coord=[1, 1, 4],
        title="Alice preference",
        domain="alice",
        enrichment=_name_enrichment("Alice"),
    )

    tree = build_persona_tree("Bob", root=isolated_memory).to_dict()

    assert tree["person_id"] == "bob"
    assert set(tree["record_ids"]) == {prior_id, new_id, event_id, relationship_id}
    assert {item["memory_id"] for item in tree["history"]} == {prior_id}
    current_ids = {item["memory_id"] for item in tree["current_facts"]}
    assert prior_id not in current_ids
    assert {new_id, event_id, relationship_id}.issubset(current_ids)
    assert [item["memory_id"] for item in tree["events"]] == [event_id]
    assert tree["relationships"] == [
        {
            "memory_id": relationship_id,
            "relation": "mentor",
            "other_id": "operator",
            "other_name": "Operator",
        }
    ]
    assert all("Alice" not in item.get("title", "") for item in tree["current_facts"])


def test_persona_tree_skips_unverified_records(isolated_memory):
    from memory.persona_tree import build_persona_tree

    verified_id = _write_record(
        text="Bob verified fact.",
        coord=[1, 1, 1],
        title="Bob verified",
        domain="bob",
        enrichment=_name_enrichment("Bob"),
    )
    broken_id = _write_record(
        text="Bob broken fact.",
        coord=[1, 1, 2],
        title="Bob broken",
        domain="bob",
        enrichment=_name_enrichment("Bob"),
    )
    _patch_record(broken_id, provenance={"source_id": "src_missing", "span_hash": "bad"})

    tree = build_persona_tree("Bob", root=isolated_memory).to_dict()

    assert verified_id in tree["record_ids"]
    assert broken_id not in tree["record_ids"]
