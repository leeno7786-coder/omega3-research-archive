from __future__ import annotations

from memory.recall_truncation import (
    MAX_RECEIPT_CANDIDATE_IDS,
    build_truncation_receipt,
    candidate_pool_trace,
)


def test_truncation_receipt_bounds_ids_but_reconciles_full_drop_set():
    dropped = [f"mem.drop.{index:03d}" for index in range(MAX_RECEIPT_CANDIDATE_IDS + 5)]

    receipt = build_truncation_receipt("test_cap", dropped + ["mem.kept"], ["mem.kept"])

    assert receipt["stage"] == "test_cap"
    assert receipt["input_count"] == len(dropped) + 1
    assert receipt["output_count"] == 1
    assert receipt["dropped_count"] == len(dropped)
    assert receipt["dropped_candidate_ids"] == dropped[:MAX_RECEIPT_CANDIDATE_IDS]
    assert receipt["dropped_candidate_ids_omitted_count"] == 5
    assert len(receipt["dropped_candidate_ids_sha256"]) == 64
    assert candidate_pool_trace(["mem.kept"])["candidate_ids"] == ["mem.kept"]


def test_unified_taxonomy_top_k_records_candidate_ids_dropped_before_canonical_scan(
    monkeypatch,
):
    from memory.taxonomy import recall as taxonomy_recall

    base = [
        {"id": "mem.a", "type": "memory_record", "title": "A"},
        {"id": "mem.b", "type": "memory_record", "title": "B"},
        {"id": "mem.c", "type": "memory_record", "title": "C"},
    ]
    scores = {"mem.a": 0.9, "mem.b": 0.8, "mem.c": 0.7}
    monkeypatch.setattr(
        taxonomy_recall.recall_graph,
        "load_canonical_candidates",
        lambda **_kwargs: base,
    )
    monkeypatch.setattr(
        taxonomy_recall.recall_graph,
        "expand_via_graph",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        taxonomy_recall,
        "_score_candidate",
        lambda meta, _tokens, _plan: scores[meta["id"]],
    )
    monkeypatch.setattr(taxonomy_recall, "_read_snapshot_hash", lambda _root: "snapshot")
    monkeypatch.setattr(taxonomy_recall, "compile_recall_plan", lambda _query: None)

    packet = taxonomy_recall._recall_in_transaction(
        "query",
        allowed_lobes=None,
        denied_lobes=(),
        allowed_subdomains=None,
        lane_weights=None,
        limit=2,
        root=None,
        touch=False,
        query_kind=None,
        episode_id="",
        log=False,
        created_at=None,
    )

    assert [row["node_id"] for row in packet.results] == ["mem.a", "mem.b"]
    receipt = next(
        row for row in packet.truncation_receipts if row["stage"] == "taxonomy_top_k"
    )
    assert receipt["stage"] == "taxonomy_top_k"
    assert receipt["input_count"] == 3
    assert receipt["output_count"] == 2
    assert receipt["dropped_count"] == 1
    assert receipt["dropped_candidate_ids"] == ["mem.c"]
    assert packet.to_dict()["truncation_receipts"] == list(packet.truncation_receipts)


def test_unified_taxonomy_records_weak_via_link_candidates_as_dropped(monkeypatch):
    from memory.taxonomy import recall as taxonomy_recall

    base = [{"id": "mem.seed", "type": "memory_record", "title": "Query seed"}]
    linked = [
        {
            "id": "mem.unrelated",
            "type": "memory_record",
            "title": "Unrelated neighbor",
            "via_link": True,
        }
    ]
    monkeypatch.setattr(
        taxonomy_recall.recall_graph,
        "load_canonical_candidates",
        lambda **_kwargs: base,
    )
    monkeypatch.setattr(
        taxonomy_recall.recall_graph,
        "expand_via_graph",
        lambda *_args, **_kwargs: linked,
    )
    monkeypatch.setattr(taxonomy_recall, "_hop_survives", lambda _meta, _tokens: False)
    monkeypatch.setattr(taxonomy_recall, "_score_candidate", lambda *_args: 1.0)
    monkeypatch.setattr(taxonomy_recall, "_read_snapshot_hash", lambda _root: "snapshot")
    monkeypatch.setattr(taxonomy_recall, "compile_recall_plan", lambda _query: None)

    packet = taxonomy_recall._recall_in_transaction(
        "query",
        allowed_lobes=None,
        denied_lobes=(),
        allowed_subdomains=None,
        lane_weights=None,
        limit=5,
        root=None,
        touch=False,
        query_kind=None,
        episode_id="",
        log=False,
        created_at=None,
    )

    assert [row["node_id"] for row in packet.results] == ["mem.seed"]
    receipt = next(
        row
        for row in packet.truncation_receipts
        if row["stage"] == "taxonomy_via_link_content"
    )
    assert receipt["input_count"] == 2
    assert receipt["output_count"] == 1
    assert receipt["dropped_candidate_ids"] == ["mem.unrelated"]


def test_unknown_zero_weight_lane_is_not_silently_ignored(monkeypatch):
    import pytest

    from memory.taxonomy import recall as taxonomy_recall

    monkeypatch.setattr(taxonomy_recall, "compile_recall_plan", lambda _query: None)
    with pytest.raises(ValueError, match="unknown recall lane.*mystery"):
        taxonomy_recall._recall_in_transaction(
            "query",
            allowed_lobes=None,
            denied_lobes=(),
            allowed_subdomains=None,
            lane_weights={"mystery": 0},
            limit=5,
            root=None,
            touch=False,
            query_kind=None,
            episode_id="",
            log=False,
            created_at=None,
        )
