"""The temporal lane's recent_first re-rank orders by the monotonic write-ordinal
(memory.write_ordinal), NOT the deterministic content-hash ingested_at — that hash
carries no chronological meaning, so "what did you learn last" must key on the real
write order. Legacy records without an ordinal sort last.
"""

from __future__ import annotations

import memory.recall_lanes as rl
from memory.deterministic import stable_iso


class _Posture:
    def __init__(self, bias):
        self.temporal_bias = bias


class _Plan:
    def __init__(self, bias):
        self.posture = _Posture(bias)


def _hits(*ids):
    return [rl.LaneHit(memory_id=i, lane=rl.LANE_TEMPORAL, score=0.5, reason="t") for i in ids]


def _lane(monkeypatch, base_ids, provenance_by_id, bias):
    monkeypatch.setattr(rl, "_run_lobe_lane", lambda *a, **k: _hits(*base_ids))
    snapshot = {i: {"path": i} for i in base_ids}
    def load_meta(path):
        return {"provenance": provenance_by_id.get(path, {})}
    return rl._run_temporal_lane(_Plan(bias), snapshot, load_meta, set())


def _lane_with_records(monkeypatch, base_ids, records, bias):
    monkeypatch.setattr(rl, "_run_lobe_lane", lambda *a, **k: _hits(*base_ids))
    snapshot = {i: {"path": i} for i in base_ids}
    return rl._run_temporal_lane(_Plan(bias), snapshot, records.get, set())


def test_recent_first_orders_by_write_ordinal_desc(monkeypatch):
    prov = {"a": {"write_ordinal": 1}, "b": {"write_ordinal": 3}, "c": {"write_ordinal": 2}}
    out = _lane(monkeypatch, ["a", "b", "c"], prov, "recent_first")
    assert [h.memory_id for h in out] == ["b", "c", "a"]


def test_ordinal_beats_hash_ingested_at(monkeypatch):
    # ingested_at (deterministic hash) points the OPPOSITE way; the ordinal wins.
    prov = {
        "a": {"write_ordinal": 9, "ingested_at": "2001-01-01T00:00:00+00:00"},
        "b": {"write_ordinal": 1, "ingested_at": "2099-01-01T00:00:00+00:00"},
    }
    out = _lane(monkeypatch, ["a", "b"], prov, "recent_first")
    assert [h.memory_id for h in out] == ["a", "b"]  # a is most-recently written


def test_legacy_records_without_ordinal_sort_last(monkeypatch):
    prov = {"a": {"write_ordinal": 5}, "b": {}, "c": {"write_ordinal": 3}}
    out = _lane(monkeypatch, ["a", "b", "c"], prov, "recent_first")
    assert [h.memory_id for h in out] == ["a", "c", "b"]


def test_legacy_content_hash_time_has_no_recency_authority(monkeypatch):
    legacy = stable_iso(
        "memory.core.ingested_at",
        "legacy",
        [9, 1, 0],
        "operator_chat",
        "legacy-id",
    )
    records = {
        "legacy": {
            "id": "legacy-id",
            "coord": [9, 1, 0],
            "text": "legacy",
            "provenance": {"author": "operator_chat", "ingested_at": legacy},
        },
        "observed": {
            "id": "observed-id",
            "coord": [9, 1, 0],
            "text": "observed",
            "provenance": {
                "author": "operator_chat",
                "ingested_at": "2026-01-01T00:00:00+00:00",
            },
        },
    }

    out = _lane_with_records(
        monkeypatch, ["legacy", "observed"], records, "recent_first"
    )

    assert [hit.memory_id for hit in out] == ["observed", "legacy"]


def test_no_recency_bias_preserves_base_order(monkeypatch):
    prov = {"a": {"write_ordinal": 1}, "b": {"write_ordinal": 3}}
    out = _lane(monkeypatch, ["a", "b"], prov, None)
    assert [h.memory_id for h in out] == ["a", "b"]  # base order, no re-rank
