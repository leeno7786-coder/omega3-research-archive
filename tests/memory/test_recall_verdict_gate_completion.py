from __future__ import annotations

import json

import pytest

from memory.api import QuerySpec, read_memory, recall_verified
from memory.recall_verifier import verify_candidates


def _write_record(
    tmp_path,
    memory_id: str,
    *,
    title: str,
    summary: str,
    coord=None,
    provenance=None,
    status=None,
    cite=True,
):
    coord = coord or [2, 0, 1]
    text = f"{title}. {summary}"
    prov = dict(provenance or {})
    # Cite by default (post 2026-07-21 fail-closed): recall verifies via ledger
    # crypto, not rederive. Pass cite=False for the unprovenanced/unknown path.
    if cite and not prov.get("source_id"):
        from memory.source_ledger.store import record_source

        artifact = record_source(
            text, source_type="operator_chat", origin_id=f"cycle-{memory_id}", root=tmp_path
        )
        prov.update(artifact.citation())
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    record = {
        "id": memory_id,
        "coord": coord,
        "title": title,
        "summary": summary,
        "text": text,
        "provenance": prov,
    }
    if status:
        record["status"] = status
    record_path = memory_root / "200_knowledge" / f"{memory_id}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    coord_index = {}
    if index_path.exists():
        coord_index = json.loads(index_path.read_text(encoding="utf-8"))
    coord_index[memory_id] = {"coord": coord, "path": str(record_path.relative_to(tmp_path))}
    index_path.write_text(json.dumps(coord_index), encoding="utf-8")


@pytest.fixture
def memory_roots(tmp_path, monkeypatch):
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.recall_verifier as recall_verifier

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(recall_verifier, "OMEGA_ROOT", tmp_path)
    return tmp_path


def test_read_memory_filters_unverified_candidates(memory_roots, monkeypatch):
    tmp_path = memory_roots
    # cite=False: an unprovenanced record is unverified — read_memory must filter it.
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada", cite=False)

    class FakePacket:
        results = [
            {
                "node_id": "mem.creator",
                "score": 0.9,
                "label": "Wrong Title",
                "payload": {"coord": [2, 0, 1], "summary": "Built by Ada"},
                "evidence_event_ids": ["canonical:mem.creator"],
            }
        ]
        snapshot_hash = "test"
        resolved_entities = ()
        evidence_event_ids = ["canonical:mem.creator"]

    monkeypatch.setattr("memory.taxonomy.recall.recall", lambda **kwargs: FakePacket())
    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", lambda query: None)

    result = read_memory(QuerySpec(cycle_id="cycle-gate", text="creator", limit=5))
    verdict = (result.meta or {}).get("recall_verdict") or {}

    assert result.candidates == []
    assert verdict.get("status") == "unknown"


def test_read_memory_does_not_mutate_unverified_candidates(memory_roots, monkeypatch):
    tmp_path = memory_roots
    # cite=False: an unprovenanced (unverified) candidate must not be mutated.
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada", cite=False)

    class FakePacket:
        results = [
            {
                "node_id": "mem.creator",
                "score": 0.9,
                "label": "Wrong Title",
                "payload": {"coord": [2, 0, 1], "summary": "Built by Ada"},
                "evidence_event_ids": ["canonical:mem.creator"],
            }
        ]
        snapshot_hash = "test"
        resolved_entities = ()
        evidence_event_ids = ["canonical:mem.creator"]

    def fail_side_effect(*_args, **_kwargs):
        raise AssertionError("unverified recall must not mutate memory state")

    class FailingStrengthIndex:
        def touch(self, *_args, **_kwargs):
            raise AssertionError("unverified recall must not touch StrengthIndex")

    monkeypatch.setattr("memory.taxonomy.recall.recall", lambda **kwargs: FakePacket())
    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", lambda query: None)
    monkeypatch.setattr("memory.confidence_decay.apply_recall_confidence_decay", fail_side_effect)
    monkeypatch.setattr("memory.demotion.apply_recall_sustained_floor_demotion", fail_side_effect)
    monkeypatch.setattr(
        "memory.strength_index.ensure_strength_index", lambda: FailingStrengthIndex()
    )

    result = read_memory(QuerySpec(cycle_id="cycle-no-mutate", text="creator", limit=5))

    assert result.candidates == []
    assert result.meta["recall_verdict"]["status"] == "unknown"
    assert result.meta["touched"] == 0
    assert result.meta["confidence_decayed"] == 0
    assert result.meta["sustained_floor_demoted"] == 0


def test_read_memory_defers_verified_reconsolidation_until_episode_deepsleep(
    memory_roots, monkeypatch
):
    tmp_path = memory_roots
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada")

    class FakePacket:
        results = [
            {
                "node_id": "mem.creator",
                "score": 0.9,
                "label": "Creator",
                "payload": {"coord": [2, 0, 1], "summary": "Built by Ada"},
                "evidence_event_ids": ["canonical:mem.creator"],
            }
        ]
        snapshot_hash = "test"
        resolved_entities = ()
        evidence_event_ids = ["canonical:mem.creator"]

    calls = {"decay": [], "demotion": [], "touch": []}

    class RecordingStrengthIndex:
        def touch(self, memory_id):
            calls["touch"].append(memory_id)

    monkeypatch.setattr("memory.taxonomy.recall.recall", lambda **kwargs: FakePacket())
    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", lambda query: None)
    monkeypatch.setattr(
        "memory.confidence_decay.apply_recall_confidence_decay",
        lambda memory_id: calls["decay"].append(memory_id) or {"applied": True},
    )
    monkeypatch.setattr(
        "memory.demotion.apply_recall_sustained_floor_demotion",
        lambda memory_id: calls["demotion"].append(memory_id) or {"demoted": True},
    )
    monkeypatch.setattr(
        "memory.strength_index.ensure_strength_index", lambda: RecordingStrengthIndex()
    )

    from memory import episode_sram

    episode_sram.open_bucket("episode-mutate", pipeline_id=0)
    try:
        query = QuerySpec(
            cycle_id="episode-mutate-mril-remember_knowledge",
            text="creator",
            limit=5,
        )
        # Production recall operations need their own unique cycle id for telemetry,
        # while DeepSleep maintenance belongs to the episode-level SRAM bucket.
        query.episode_id = "episode-mutate"
        result = read_memory(query)

        assert [c.id for c in result.candidates] == ["mem.creator"]
        assert calls == {"decay": [], "demotion": [], "touch": []}
        assert result.meta["recall_maintenance_deferred"] == 1

        episode_sram.close_bucket("episode-mutate")
        assert calls == {
            "decay": ["mem.creator"],
            "demotion": ["mem.creator"],
            "touch": ["mem.creator"],
        }
    finally:
        episode_sram.clear_bucket("episode-mutate")


def test_verify_candidates_returns_conflict_for_same_title_different_summaries(memory_roots):
    tmp_path = memory_roots
    _write_record(
        tmp_path, "mem.a", title="Favorite color", summary="Blue is best", coord=[2, 0, 1]
    )
    _write_record(
        tmp_path, "mem.b", title="Favorite color", summary="Green is best", coord=[2, 0, 2]
    )
    verdict = verify_candidates(
        "favorite color",
        [
            {
                "memory_id": "mem.a",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Favorite color",
                "summary": "Blue is best",
            },
            {
                "memory_id": "mem.b",
                "coord": [2, 0, 2],
                "score": 0.8,
                "title": "Favorite color",
                "summary": "Green is best",
            },
        ],
        root=tmp_path,
    )
    assert verdict.status == "conflict"


def test_recall_graph_skips_quarantined_records(memory_roots, monkeypatch):
    tmp_path = memory_roots
    _write_record(tmp_path, "mem.bad", title="Agent", summary="Bad record", status="quarantined")
    index = json.loads(
        (tmp_path / "memory" / "indexes" / "coord_index.json").read_text(encoding="utf-8")
    )
    monkeypatch.setattr("memory.taxonomy.recall_graph.coord_index_snapshot", lambda: index)
    from memory.taxonomy.recall_graph import load_canonical_candidates

    out = load_canonical_candidates(root=tmp_path)
    assert out == []


def test_build_memory_dict_stamps_source_ledger(memory_roots):
    tmp_path = memory_roots
    from memory.core import build_memory_dict

    mem = build_memory_dict(
        text="Canonical records cite immutable source artifacts.",
        coord=[2, 0, 1],
        source_tag="omega:test",
        metadata={
            "title": "Ledger-backed memory",
            "summary": "Canonical records cite immutable source artifacts.",
        },
    )

    provenance = mem.get("provenance") or {}
    assert provenance.get("source_id", "").startswith("src_")
    assert (
        tmp_path / "memory" / "source_ledger" / "artifacts" / f"{provenance['source_id']}.json"
    ).exists()


def test_recall_verified_does_not_double_gate(monkeypatch):
    calls = {"read": 0, "verify": 0}

    def fake_retrieve(query):
        calls["read"] += 1

        class R:
            candidates = []
            meta = {}

        return R()

    def fake_verify(query, result, **kwargs):
        calls["verify"] += 1
        from memory.taxonomy.schemas import RecallVerdict

        return RecallVerdict(status="unknown", query=query, reason="none")

    monkeypatch.setattr("memory.api._retrieve_memory", fake_retrieve)
    monkeypatch.setattr("memory.recall_verifier.verify_retrieval_result", fake_verify)
    monkeypatch.setattr("memory.recall_verifier.append_verdict_log", lambda *a, **k: {})

    recall_verified(QuerySpec(cycle_id="cycle-one", text="hello"))
    assert calls["read"] == 1
    assert calls["verify"] == 1


def test_production_recall_callers_do_not_bypass_public_verdict_gate():
    from pathlib import Path

    root = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
    production_roots = [
        root / "hud",
        root / "auto_loop",
        root / "pressure_loop",
        root / "omega_loop",
    ]
    offenders = []
    for base in production_roots:
        for path in sorted(base.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if (
                "verdict_gate=False" in text
                or "_retrieve_memory(" in text
                or "walker_query(" in text
            ):
                offenders.append(path.relative_to(root).as_posix())

    assert offenders == []


def test_verdict_gate_preserves_pipeline_fields(monkeypatch):
    """The gate rebuilds candidates from the verifier payload — it must
    carry over the enrichment fields the recall pipeline attached to the
    pre-gate candidates (rerank_score alias, link-walk and flag markers,
    supersedes history), keyed by memory id. Silent field loss here is
    how cand.rerank_score came back None after the verifier era
    (test_walker_recall 4e, rerank_score=-1.000)."""
    from types import SimpleNamespace

    from memory.api import RetrievalCandidate, RetrievalResult, _apply_recall_verdict_gate

    pre = RetrievalCandidate(
        id="2.0.k1",
        coord=[2, 0, 1],
        score=0.8,
        provenance={"path": "x"},
        title="Creator",
        summary="Built by Ada",
        domain="knowledge",
    )
    pre.rerank_score = 0.71
    pre.via_link = True
    pre.flagged = True
    pre.flag_reason = "test-flag"
    pre.flag_response_mode = "bounded"
    pre.history = [{"id": "2.0.k0", "version": 1, "title": "Creator (old)"}]
    pre.version = 2

    rejected_pre = RetrievalCandidate(
        id="2.0.k2",
        coord=[2, 0, 2],
        score=0.7,
        title="Creator",
        summary="Another creator fact",
    )

    result = RetrievalResult(
        cycle_id="cycle-fields",
        candidates=[pre, rejected_pre],
        meta={},
    )

    verdict = SimpleNamespace(
        status="verified",
        reason="",
        rejected_hits=[
            {
                "memory_id": "2.0.k2",
                "status": "unknown",
                "reason": "canonical file missing",
            }
        ],
        retrieval_trace={
            "candidate_accounting": {
                "scope": "pre_verifier_candidate_pool",
                "retrieved_count": 2,
                "admitted_count": 1,
                "rejected_count": 1,
            }
        },
        answer_payload={
            "candidates": [
                {
                    "memory_id": "2.0.k1",
                    "coord": [2, 0, 1],
                    "score": 0.8,
                    "title": "Creator",
                    "summary": "Built by Ada",
                    "domain": "knowledge",
                    "source_proof": {"evidence_strength": "direct"},
                    "path": "x",
                    "sense_ids": [],
                }
            ]
        },
        to_dict=lambda: {"status": "verified"},
    )
    monkeypatch.setattr(
        "memory.recall_verifier.verify_retrieval_result",
        lambda text, res, **kwargs: verdict,
    )
    monkeypatch.setattr(
        "memory.recall_verifier.append_verdict_log",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "memory.api._apply_verified_recall_side_effects",
        lambda *, cycle_id, episode_id, verified_raw, meta: meta,
    )
    emitted = []
    monkeypatch.setattr(
        "guardian.terminal_feedback.emit_data",
        lambda system, event, **fields: emitted.append((system, event, fields)),
    )

    gated = _apply_recall_verdict_gate(
        QuerySpec(cycle_id="cycle-fields", text="creator", limit=5), result
    )

    assert len(gated.candidates) == 1
    cand = gated.candidates[0]
    assert cand.rerank_score == pytest.approx(0.71)
    assert cand.via_link is True
    assert cand.flagged is True
    assert cand.flag_reason == "test-flag"
    assert cand.flag_response_mode == "bounded"
    assert cand.history and cand.history[0]["id"] == "2.0.k0"
    assert cand.version == 2
    telemetry = next(
        fields
        for system, event, fields in emitted
        if system == "memory" and event == "read_memory_verdict"
    )
    assert telemetry["status"] == "verified"
    assert telemetry["retrieved_count"] == 2
    assert telemetry["admitted_count"] == 1
    assert telemetry["rejected_count"] == 1
