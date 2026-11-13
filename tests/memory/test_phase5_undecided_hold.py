"""Phase 5 — _unsorted holding subdomain + entity-link re-key tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from memory.dewey_map import UNSORTED_SUBDOMAIN_Y
from memory.gates.undecided_hold import route_undecided_subdomain


def test_route_undecided_static_lobe_no_grammar():
    coord, label_id, undecided = route_undecided_subdomain(
        "blizzard penguin teapot",
        3,
        [3, 0, 0],
        "3.00",
        dynamic_y_lobes=frozenset(),
    )
    assert undecided is True
    assert coord == [3, UNSORTED_SUBDOMAIN_Y, 0]
    assert label_id == f"3.{UNSORTED_SUBDOMAIN_Y:02d}"


def test_route_undecided_skipped_when_grammar_hits():
    coord, label_id, undecided = route_undecided_subdomain(
        "what are the constraints",
        3,
        [3, 0, 0],
        "3.00",
        dynamic_y_lobes=frozenset(),
    )
    assert undecided is False
    assert coord == [3, 0, 0]


def test_route_undecided_skipped_for_dynamic_lobe():
    coord, _, undecided = route_undecided_subdomain(
        "blizzard penguin teapot",
        2,
        [2, 0, 0],
        "2.00",
        dynamic_y_lobes=frozenset({2, 8}),
    )
    assert undecided is False
    assert coord == [2, 0, 0]


def test_reasoning_gate_undecided_ingest_writes_unsorted(monkeypatch):
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import ReasoningGate

    captured = {}

    def _capture_atomic_write(meta, update_index=True):
        captured["meta"] = dict(meta)
        return f"<test>/{meta['id']}.json"

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
    ):
        result = ReasoningGate().ingest(
            "blizzard penguin teapot",
            metadata={"type": "concept", "title": "test/reasoning/x"},
        )

    assert result is not None
    assert captured["meta"]["coord"] == [3, UNSORTED_SUBDOMAIN_Y, 0]
    assert captured["meta"]["provenance"]["undecided"] is True
    assert captured["meta"]["provenance"]["confidence"] <= 0.35


def test_entity_link_keys_from_situational_linked_persons():
    from memory.enrichment.entity_linker import extract_link_keys_from_memory

    keys = extract_link_keys_from_memory(
        {
            "coord": [4, 0, 0],
            "enrichment": {"linked_persons": ["Alice Smith"]},
        }
    )
    assert "alice_smith" in keys


def test_entity_link_keys_from_persona_domain():
    from memory.enrichment.entity_linker import extract_link_keys_from_memory

    keys = extract_link_keys_from_memory(
        {
            "coord": [1, 1, 0],
            "domain": "bob",
            "enrichment": {"entities": [{"facet": "name", "name": "Bob"}]},
        }
    )
    assert "bob" in keys


def test_entity_link_keys_ignore_capitalized_regex_without_structure():
    from memory.enrichment.entity_linker import extract_link_keys_from_memory

    keys = extract_link_keys_from_memory(
        {
            "coord": [4, 0, 0],
            "text": "Python met Chicago yesterday",
        }
    )
    assert keys == set()


def test_link_personal_memories_uses_person_id_not_proper_noun_regex(tmp_path, monkeypatch):
    from memory.enrichment import entity_linker as el
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path)

    mem_a = tmp_path / "a.json"
    mem_b = tmp_path / "b.json"
    mem_a.write_text(
        '{"id":"a1","coord":[4,0,0],"enrichment":{"linked_persons":["alice"]},"links":[]}',
        encoding="utf-8",
    )
    mem_b.write_text(
        '{"id":"b1","coord":[1,1,0],"domain":"alice","links":[]}',
        encoding="utf-8",
    )
    index = {
        "a1": {"coord": [4, 0, 0], "path": str(mem_a)},
        "b1": {"coord": [1, 1, 0], "path": str(mem_b)},
    }
    new_mem = tmp_path / "new.json"
    new_mem.write_text(
        '{"id":"n1","coord":[4,6,0],"enrichment":{"linked_persons":["alice"]},"text":"Python Chicago","links":[]}',
        encoding="utf-8",
    )
    created = el.link_personal_memories(str(new_mem), index)
    assert created >= 1
    import json

    updated = json.loads(new_mem.read_text(encoding="utf-8"))
    assert "b1" in updated.get("links", [])


def test_subdomain_map_unsorted_entries_for_static_lobes():
    from memory.dewey_map import SUBDOMAIN_MAP, UNSORTED_SUBDOMAIN_Y

    static_lobes = (0, 1, 3, 4, 5, 6, 7, 9, 10, 11)
    for lobe in static_lobes:
        assert SUBDOMAIN_MAP.get((lobe, UNSORTED_SUBDOMAIN_Y)) == "_unsorted"
    assert (2, UNSORTED_SUBDOMAIN_Y) not in SUBDOMAIN_MAP
    assert (8, UNSORTED_SUBDOMAIN_Y) not in SUBDOMAIN_MAP


def test_journal_lobe_grammar_always_decides_never_unsorted():
    """Lobe 6 helper always returns Y=0 — _unsorted hold is unreachable by design."""
    from auto_loop.mril_tools.subdomain_helpers import subdomain_for

    assert subdomain_for("any journal text", 6) == 0
    coord, _, undecided = route_undecided_subdomain(
        "any journal text",
        6,
        [6, 0, 0],
        "6.00",
        dynamic_y_lobes=frozenset(),
    )
    assert undecided is False
    assert coord == [6, 0, 0]


def test_reasoning_gate_recall_finds_unsorted_via_lobe_fanout(monkeypatch):
    """Phase 3 whole-lobe fan-out must include Y=127 _unsorted candidates."""
    from memory.engine import walker as W
    from memory.gates.lobe_gates import ReasoningGate

    mem_id = "reason_unsorted_1"
    captured_path = "/fake/reason_unsorted.json"
    text = "blizzard penguin teapot reasoning hold marker"

    fake_index = {
        mem_id: {"coord": [3, UNSORTED_SUBDOMAIN_Y, 0], "path": captured_path},
        "other": {"coord": [3, 0, 0], "path": "/fake/goals.json"},
    }

    def fake_load_meta(path):
        if "unsorted" in path:
            return {
                "id": mem_id,
                "coord": [3, UNSORTED_SUBDOMAIN_Y, 0],
                "title": "held",
                "summary": text,
                "text": text,
                "provenance": {"confidence": 0.3, "undecided": True},
                "tags": [],
            }
        return {
            "id": "other",
            "coord": [3, 0, 0],
            "title": "goals",
            "summary": "unrelated goals objective",
            "text": "",
            "provenance": {"confidence": 0.9},
            "tags": [],
        }

    monkeypatch.setattr(W, "COORD_INDEX", fake_index)
    monkeypatch.setattr(W, "load_coord_index", lambda: fake_index)
    monkeypatch.setattr(W, "load_meta_by_path", fake_load_meta)

    hits = ReasoningGate().recall(text, limit=5)
    assert hits, "whole-lobe fan-out must return _unsorted record"
    assert hits[0]["coord"] == [3, UNSORTED_SUBDOMAIN_Y, 0]


def test_undecided_write_read_coord_symmetry_smoke(monkeypatch):
    """Spec §7: undecided write coord must match recall top-hit for same text."""
    from memory.gates import base as gate_base
    from memory.gates.lobe_gates import ReasoningGate
    from memory.engine import walker as W

    text = "blizzard penguin teapot symmetry hold"
    mem_id = "undecided_symmetry_1"
    captured_path = "/fake/undecided_symmetry.json"
    expected_coord = [3, UNSORTED_SUBDOMAIN_Y, 0]

    captured = {}

    def _capture_atomic_write(meta, update_index=True):
        captured["meta"] = dict(meta)
        return captured_path

    fake_index = {
        mem_id: {"coord": expected_coord, "path": captured_path},
    }

    def fake_load_meta(path):
        return {
            "id": mem_id,
            "coord": expected_coord,
            "title": "held",
            "summary": text,
            "text": text,
            "provenance": {"confidence": 0.3, "undecided": True},
            "tags": [],
        }

    monkeypatch.setattr(W, "COORD_INDEX", fake_index)
    monkeypatch.setattr(W, "load_coord_index", lambda: fake_index)
    monkeypatch.setattr(W, "load_meta_by_path", fake_load_meta)

    with (
        patch.object(gate_base, "atomic_write", side_effect=_capture_atomic_write),
        patch.object(gate_base, "_emit", lambda *a, **k: None),
        patch("memory.subdomain_flag.dynamic_y_lobes", return_value=frozenset()),
    ):
        assert (
            ReasoningGate().ingest(
                text,
                metadata={"type": "concept", "title": "test/reasoning/x"},
            )
            is not None
        )

    assert captured["meta"]["coord"] == expected_coord
    assert captured["meta"]["provenance"]["undecided"] is True

    hits = ReasoningGate().recall(text, limit=5)
    assert hits, "recall must return the undecided record"
    assert hits[0]["coord"] == expected_coord
