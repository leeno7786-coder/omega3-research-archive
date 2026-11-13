"""Phase 7B — memory taxonomy schemas.

The canonical memory object model for the deterministic taxonomy layer. House style
(schema/ idiom): frozen dataclass + module-level frozenset enums + validate()->list[str]
(never raises, [] == valid) + hand-written to_dict() (type-first, schema_version-last).

These schemas are sidecar PROJECTION records (not cognition-engine I/O contracts), built
Guardian-free. They live under memory/taxonomy/ per the Phase 7 work order.
"""

from __future__ import annotations

import json

import pytest

from memory.taxonomy import schemas as S


# ── helpers: a valid instance of each schema ───────────────────────────────────
def _event(**over):
    base = dict(
        event_id="evt_abc",
        event_kind="qwen_skill_result",
        episode_id="ep1",
        source="auto_loop.fold",
        created_at="2026-06-21T00:00:00Z",
        content_hash="deadbeef",
        payload={"status": "success"},
    )
    base.update(over)
    return S.MemoryEvent(**base)


def _entity(**over):
    base = dict(
        entity_id="ent_skill_x",
        entity_kind="skill",
        canonical_name="codex-grade-coding",
        aliases=("codex",),
        source_event_id="evt_abc",
    )
    base.update(over)
    return S.MemoryEntity(**base)


def _claim(**over):
    base = dict(
        claim_id="clm_1",
        subject="codex-grade-coding",
        predicate="selected_for",
        object="inspect the dispatch schema",
        confidence=1.0,
        status="active",
        source_event_id="evt_abc",
        evidence_event_ids=("evt_abc",),
    )
    base.update(over)
    return S.MemoryClaim(**base)


def _edge(**over):
    base = dict(
        edge_id="edg_1",
        source_id="ep1",
        target_id="ent_skill_x",
        edge_type="SELECTED_SKILL",
        confidence=1.0,
        evidence_event_id="evt_abc",
    )
    base.update(over)
    return S.MemoryEdge(**base)


def _snapshot(**over):
    base = dict(
        snapshot_hash="abc123",
        event_count=10,
        entity_count=5,
        claim_count=7,
        node_count=12,
        edge_count=9,
        compiler_version="omega.memory_compiler.v1",
        schema_version_compiled="omega.memory_event.v1",
        config_hash="cfg1",
    )
    base.update(over)
    return S.MemoryGraphSnapshot(**base)


def _packet(**over):
    base = dict(
        query="what skill inspected the dispatch schema",
        query_hash="qh1",
        query_kind="skill_use",
        resolved_entities=("ent_skill_x",),
        results=({"node_id": "n1", "score": 0.9, "evidence_event_ids": ["evt_abc"]},),
        snapshot_hash="abc123",
        evidence_event_ids=("evt_abc",),
    )
    base.update(over)
    return S.RecallPacket(**base)


def _leaf(**over):
    base = dict(
        memory_id="2.00.0001",
        coord=(2, 0, 1),
        title="Photosynthesis",
        summary="Plants convert light into chemical energy.",
        source_id="src_abc123",
        source_type="operator_chat",
        span_start=0,
        span_end=43,
        span_hash="beadfeed",
        sense_ids=("sense:photosynthesis:biology",),
        status="active",
        domain="biology",
        subtopic="plants",
        tags=("biology", "energy"),
    )
    base.update(over)
    return S.MemoryLeafV2(**base)


# ── MemoryEvent ────────────────────────────────────────────────────────────────
def test_memory_event_valid():
    assert _event().validate() == []


def test_memory_event_missing_event_id_fails():
    errs = _event(event_id="").validate()
    assert any("event_id" in e for e in errs)


def test_memory_event_invalid_event_kind_fails():
    errs = _event(event_kind="not_a_real_kind").validate()
    assert any("event_kind" in e for e in errs)


def test_memory_event_all_28_kinds_accepted():
    for kind in S.EVENT_KINDS:
        assert _event(event_kind=kind).validate() == [], kind


def test_memory_event_to_dict_type_first_version_last():
    d = _event().to_dict()
    keys = list(d.keys())
    assert keys[0] == "type"
    assert keys[-1] == "schema_version"
    assert d["type"] == "memory_event"
    assert d["schema_version"] == S.MEMORY_EVENT_SCHEMA_VERSION


# ── MemoryEntity ─────────────────────────────────────────────────────────────--
def test_memory_entity_valid():
    assert _entity().validate() == []


def test_memory_entity_invalid_kind_fails():
    errs = _entity(entity_kind="galaxy").validate()
    assert any("entity_kind" in e for e in errs)


def test_memory_entity_missing_canonical_name_fails():
    errs = _entity(canonical_name="").validate()
    assert any("canonical_name" in e for e in errs)


def test_memory_entity_missing_source_event_id_fails():
    errs = _entity(source_event_id="").validate()
    assert any("source_event_id" in e for e in errs)


def test_memory_entity_aliases_serialize_to_list():
    assert _entity().to_dict()["aliases"] == ["codex"]


# ── MemoryClaim ──────────────────────────────────────────────────────────────--
def test_memory_claim_valid():
    assert _claim().validate() == []


def test_memory_claim_missing_evidence_fails():
    errs = _claim(evidence_event_ids=()).validate()
    assert any("evidence" in e for e in errs)


def test_memory_claim_invalid_status_fails():
    errs = _claim(status="maybe").validate()
    assert any("status" in e for e in errs)


def test_memory_claim_missing_source_event_id_fails():
    errs = _claim(source_event_id="").validate()
    assert any("source_event_id" in e for e in errs)


def test_memory_claim_confidence_out_of_range_fails():
    assert any("confidence" in e for e in _claim(confidence=1.5).validate())
    assert any("confidence" in e for e in _claim(confidence=-0.1).validate())


def test_memory_claim_all_statuses_accepted():
    for st in S.CLAIM_STATUSES:
        assert _claim(status=st).validate() == [], st


# ── MemoryEdge ───────────────────────────────────────────────────────────────--
def test_memory_edge_valid():
    assert _edge().validate() == []


def test_memory_edge_invalid_type_fails():
    errs = _edge(edge_type="POINTS_AT").validate()
    assert any("edge_type" in e for e in errs)


def test_memory_edge_missing_evidence_fails():
    errs = _edge(evidence_event_id="").validate()
    assert any("evidence_event_id" in e for e in errs)


def test_memory_edge_all_types_accepted():
    for et in S.EDGE_TYPES:
        assert _edge(edge_type=et).validate() == [], et


# ── MemoryGraphSnapshot ──────────────────────────────────────────────────────--
def test_memory_graph_snapshot_valid():
    assert _snapshot().validate() == []


def test_memory_graph_snapshot_missing_hash_fails():
    assert any("snapshot_hash" in e for e in _snapshot(snapshot_hash="").validate())


def test_memory_graph_snapshot_negative_count_fails():
    assert any("count" in e for e in _snapshot(event_count=-1).validate())


# ── RecallPacket ─────────────────────────────────────────────────────────────--
def test_recall_packet_valid():
    assert _packet().validate() == []


def test_recall_packet_missing_snapshot_hash_fails():
    assert any("snapshot_hash" in e for e in _packet(snapshot_hash="").validate())


def test_recall_packet_results_without_evidence_fails():
    # doctrine: every recalled packet with results must cite evidence event IDs
    errs = _packet(evidence_event_ids=()).validate()
    assert any("evidence" in e for e in errs)


def test_recall_packet_empty_results_no_evidence_ok():
    assert _packet(results=(), evidence_event_ids=()).validate() == []


# ── MemoryLeafV2 ──────────────────────────────────────────────────────────────
def test_memory_leaf_v2_valid_and_carries_source_span_fields():
    leaf = _leaf()
    assert leaf.validate() == []
    d = leaf.to_dict()
    assert d["type"] == "memory_leaf_v2"
    assert d["memory_id"] == "2.00.0001"
    assert d["coord"] == [2, 0, 1]
    assert d["source_id"] == "src_abc123"
    assert d["source_type"] == "operator_chat"
    assert d["span_start"] == 0
    assert d["span_end"] == 43
    assert d["span_hash"] == "beadfeed"
    assert d["sense_ids"] == ["sense:photosynthesis:biology"]
    assert d["schema_version"] == S.MEMORY_LEAF_V2_SCHEMA_VERSION


def test_memory_leaf_v2_requires_memory_id_coord_and_valid_status():
    assert any("memory_id" in e for e in _leaf(memory_id="").validate())
    assert any("coord" in e for e in _leaf(coord=(2, 0)).validate())
    assert any("status" in e for e in _leaf(status="maybe").validate())


# ── node payload schemas (Episode / Action / Outcome / Question / Failure / SkillUse) ─
def test_memory_episode_valid_and_cites_evidence():
    ep = S.MemoryEpisode(
        episode_id="ep1",
        intent="USE_SKILL",
        action="USE_SKILL",
        outcome="success",
        cascade_status="completed",
        opened_by="operator_command",
        source_event_id="evt_abc",
    )
    assert ep.validate() == []
    assert any(
        "source_event_id" in e
        for e in S.MemoryEpisode(
            episode_id="ep1",
            intent="",
            action="",
            outcome="",
            cascade_status="",
            opened_by="",
            source_event_id="",
        ).validate()
    )


def test_memory_skill_use_valid_and_carries_workflow_fields():
    su = S.MemorySkillUse(
        skill_name="codex-grade-coding",
        skill_id="5.01.qwenskill-codex-grade-coding",
        execution_kind="INSPECT",
        objective="inspect the dispatch schema",
        status="success",
        tool_sequence=("read_file", "run_command"),
        files_changed=("schema/x.py",),
        tests_run=("tests/test_x.py",),
        outcome="success",
        episode_id="ep1",
        dispatch_id="dispatch_123",
        source_event_id="evt_abc",
    )
    assert su.validate() == []
    d = su.to_dict()
    assert d["tool_sequence"] == ["read_file", "run_command"]
    assert d["files_changed"] == ["schema/x.py"]


def test_memory_skill_use_missing_skill_name_fails():
    su = S.MemorySkillUse(
        skill_name="",
        skill_id="",
        execution_kind="",
        objective="",
        status="",
        tool_sequence=(),
        files_changed=(),
        tests_run=(),
        outcome="",
        episode_id="ep1",
        dispatch_id="",
        source_event_id="evt_abc",
    )
    assert any("skill_name" in e for e in su.validate())


def test_memory_failure_valid_and_cites_evidence():
    f = S.MemoryFailure(
        failure_id="fail_1",
        failure_class="forbidden_tool",
        kind="skill",
        context="forbidden tool use",
        episode_id="ep1",
        dispatch_id="dispatch_123",
        source_event_id="evt_abc",
    )
    assert f.validate() == []
    assert any(
        "source_event_id" in e
        for e in S.MemoryFailure(
            failure_id="f",
            failure_class="",
            kind="",
            context="",
            episode_id="",
            dispatch_id="",
            source_event_id="",
        ).validate()
    )


def test_memory_action_and_outcome_and_question_valid():
    a = S.MemoryAction(
        episode_id="ep1",
        action_kind="USE_SKILL",
        tool="",
        skill_name="codex-grade-coding",
        source_event_id="evt_abc",
    )
    o = S.MemoryOutcome(
        episode_id="ep1",
        status="success",
        satisfied=True,
        summary="done",
        source_event_id="evt_abc",
    )
    q = S.MemoryQuestion(
        question_id="q1",
        text="where does the schema live?",
        status="open",
        source_event_id="evt_abc",
    )
    assert a.validate() == []
    assert o.validate() == []
    assert q.validate() == []


# ── cross-schema invariants ──────────────────────────────────────────────────--
ALL_BUILDERS = [_event, _entity, _claim, _edge, _snapshot, _packet, _leaf]


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_every_schema_to_dict_is_json_serializable(build):
    json.dumps(build().to_dict())  # must not raise


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_every_schema_to_dict_has_type_and_schema_version(build):
    d = build().to_dict()
    assert d["type"]
    assert d["schema_version"].startswith("omega.")
    assert list(d.keys())[0] == "type"
    assert list(d.keys())[-1] == "schema_version"


def test_enum_sets_are_frozensets():
    assert isinstance(S.EVENT_KINDS, frozenset)
    assert isinstance(S.ENTITY_KINDS, frozenset)
    assert isinstance(S.CLAIM_STATUSES, frozenset)
    assert isinstance(S.EDGE_TYPES, frozenset)
    # spec vocab sizes
    assert "graph_snapshot" in S.EVENT_KINDS and "qwen_skill_result" in S.EVENT_KINDS
    assert {"active", "superseded", "rejected"} == set(S.CLAIM_STATUSES)
    assert "SELECTED_SKILL" in S.EDGE_TYPES and "FAILED_BECAUSE" in S.EDGE_TYPES
