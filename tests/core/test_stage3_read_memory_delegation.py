# F:/Omega3.0/tests/core/test_stage3_read_memory_delegation.py
import pytest

import memory.api as api
from memory.api import QuerySpec
from memory.taxonomy.schemas import RecallPacket


def test_read_memory_delegates_to_taxonomy_recall(monkeypatch):
    captured = {}

    def fake_recall(
        query,
        *,
        allowed_lobes=None,
        denied_lobes=(6, 11),
        allowed_subdomains=None,
        lane_weights=None,
        limit=10,
        root=None,
        touch=True,
    ):
        captured["query"] = query
        captured["allowed_lobes"] = allowed_lobes
        captured["denied_lobes"] = denied_lobes
        captured["touch"] = touch
        return RecallPacket(
            query=query,
            query_hash="h",
            query_kind="",
            snapshot_hash="s",
            resolved_entities=("2.00.0001",),
            results=(),
            evidence_event_ids=(),
        )

    import memory.taxonomy.recall as taxrecall

    monkeypatch.setattr(taxrecall, "recall", fake_recall)
    monkeypatch.setattr(api, "_require_cycle_id", lambda cid: None)

    q = QuerySpec(text="photosynthesis", cycle_id="c1", allowed_lobes=[2], limit=5)
    api.read_memory(q)
    assert captured["query"] == "photosynthesis"
    assert captured["allowed_lobes"] == [2]
    assert captured["touch"] is False  # delegated touch must be false (post-passes handle it)


def test_read_memory_lane_arbitration_runs_without_nameerror(monkeypatch, caplog):
    """The lane-arbitration post-pass must execute, not NameError.

    The Stage-3.4 delegation rewrite deleted the allowed_set/denied/
    _passes_lobe_filters definitions but left the arbitration block referencing
    them, so every recall raised 'name allowed_set is not defined' (swallowed) and
    degraded to recall_winners=[]. With the bug, the NameError at the top of the
    block short-circuits BEFORE run_recall_lanes is ever called.
    """
    import logging
    from memory.taxonomy.schemas import RecallPacket
    import memory.taxonomy.recall as taxrecall
    from memory.engine import walker as walker_mod
    import memory.recall_lanes as recall_lanes

    def fake_recall(
        query,
        *,
        allowed_lobes=None,
        denied_lobes=(6, 11),
        allowed_subdomains=None,
        lane_weights=None,
        limit=10,
        root=None,
        touch=True,
    ):
        return RecallPacket(
            query=query,
            query_hash="h",
            query_kind="",
            snapshot_hash="s",
            resolved_entities=(),
            results=(
                {
                    "node_id": "2.00.0001",
                    "node_type": "memory_record",
                    "label": "photosynthesis",
                    "score": 0.9,
                    "evidence_event_ids": (),
                    "payload": {
                        "coord": [2, 0, 0],
                        "summary": "plants convert light",
                        "tags": ["light"],
                    },
                },
            ),
            evidence_event_ids=(),
        )

    called = {"lanes": False}

    def spy_lanes(plan, snap, loader, *, truncation_receipts=None):
        called["lanes"] = True
        return []

    monkeypatch.setattr(taxrecall, "recall", fake_recall)
    monkeypatch.setattr(api, "_require_cycle_id", lambda cid: None)
    monkeypatch.setattr(walker_mod, "coord_index_snapshot", lambda: {})
    monkeypatch.setattr(recall_lanes, "run_recall_lanes", spy_lanes)

    with caplog.at_level(logging.WARNING, logger="omega.memory.api"):
        api.read_memory(QuerySpec(text="photosynthesis", cycle_id="c1", allowed_lobes=[2], limit=5))

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "is not defined" not in joined, f"lane arbitration NameError: {joined}"
    assert called["lanes"] is True, (
        "arbitration short-circuited before run_recall_lanes (NameError?)"
    )


def test_walker_query_is_marked_diagnostic(monkeypatch):
    """walker_query is demoted to a read-only diagnostic — its meta must say so,
    so a caller can distinguish it from the unified recall engine."""
    from memory.engine.walker import walker_query

    monkeypatch.setattr(api, "_require_cycle_id", lambda cid: None)
    _cands, meta = walker_query(QuerySpec(text="anything", cycle_id="c1", limit=3))
    assert meta.get("method") == "walker_diagnostic"
    assert meta.get("diagnostic") is True
