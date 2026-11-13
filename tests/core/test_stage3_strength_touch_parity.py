# F:/Omega3.0/tests/core/test_stage3_strength_touch_parity.py
import json
import pytest

from memory.taxonomy import recall as taxrecall
from memory.taxonomy import recall_graph


@pytest.fixture
def two_record_corpus(tmp_path, monkeypatch):
    records = {
        "2.00.0001": {
            "id": "2.00.0001",
            "coord": [2, 0, 0],
            "title": "photosynthesis",
            "summary": "plants convert light to energy",
            "type": "concept",
            "tags": ["photosynthesis", "plants"],
        },
        "2.00.0002": {
            "id": "2.00.0002",
            "coord": [2, 0, 0],
            "title": "respiration",
            "summary": "cells release energy",
            "type": "concept",
            "tags": ["respiration"],
        },
    }
    snapshot = {}
    for rid, rec in records.items():
        p = tmp_path / f"{rid}.json"
        p.write_text(json.dumps(rec), encoding="utf-8")
        snapshot[rid] = {
            "coord": rec["coord"],
            "path": str(p),
            "title": rec["title"],
            "type": rec["type"],
        }
    monkeypatch.setattr(recall_graph, "coord_index_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(
        recall_graph,
        "load_meta_by_path",
        lambda path: json.loads(open(path, encoding="utf-8").read()),
    )
    # No graph edges in this corpus.
    monkeypatch.setattr(recall_graph.G, "connect", lambda root=None: object())
    monkeypatch.setattr(recall_graph.G, "neighbors", lambda conn, nid, edge_types=None: [])
    return records


def test_touch_fires_for_every_recalled_id(two_record_corpus, monkeypatch):
    touched = []

    class FakeSI:
        def touch(self, mid):
            touched.append(mid)

    monkeypatch.setattr(taxrecall, "ensure_strength_index", lambda: FakeSI())

    packet = taxrecall.recall("photosynthesis plants", allowed_lobes=[2], limit=5)
    result_ids = [r["node_id"] for r in packet.results]
    assert result_ids, "expected at least one result"
    # id parity: every recalled node_id was touched (canonical id == node_id)
    assert set(touched) == set(result_ids)


def test_recall_is_pure_when_touch_disabled(two_record_corpus, monkeypatch):
    touched = []

    class FakeSI:
        def touch(self, mid):
            touched.append(mid)

    monkeypatch.setattr(taxrecall, "ensure_strength_index", lambda: FakeSI())
    taxrecall.recall("photosynthesis", allowed_lobes=[2], touch=False)
    assert touched == []
