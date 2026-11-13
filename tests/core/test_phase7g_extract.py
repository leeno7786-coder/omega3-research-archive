"""Phase 7G — deterministic entity + claim extraction (MODEL-FREE).

From MemoryEvents, extract stable entities and atomic claims via deterministic
templates only — no LLM. Every entity/claim cites its source_event_id; repeated
extraction yields identical IDs (idempotent).
"""

from __future__ import annotations

from memory.taxonomy import extract as X
from memory.taxonomy.schemas import MemoryEntity, MemoryClaim, MemoryEvent


def _event(kind, payload, eid="evt_g1", episode="ep1"):
    return MemoryEvent(
        event_id=eid,
        event_kind=kind,
        episode_id=episode,
        source="test",
        created_at="2026-06-21T12:00:00Z",
        content_hash="h",
        payload=payload,
    )


def _skill_result_event():
    return _event(
        "qwen_skill_result",
        {
            "episode_id": "ep1",
            "dispatch_id": "d1",
            "skill_name": "omega-planner-executor",
            "skill_id": "5.01.qwenskill-omega-planner-executor",
            "execution_kind": "PATCH",
            "status": "success",
            "tool_calls": [{"tool_name": "read_file"}, {"tool_name": "run_command"}],
            "files_changed": ["schema/x.py"],
            "commands_run": ["pytest"],
            "tests_run": ["tests/test_x.py"],
            "failure_class": None,
        },
    )


# ── entities ─────────────────────────────────────────────────────────────────--
def test_skill_result_extracts_core_entities():
    ents, _ = X.extract_from_event(_skill_result_event())
    by_kind = {(e.entity_kind, e.canonical_name) for e in ents}
    assert ("skill", "omega-planner-executor") in by_kind
    assert ("dispatch", "d1") in by_kind
    assert ("episode", "ep1") in by_kind
    assert ("tool", "read_file") in by_kind
    assert ("tool", "run_command") in by_kind
    assert ("file", "schema/x.py") in by_kind
    assert all(isinstance(e, MemoryEntity) and e.validate() == [] for e in ents)
    assert all(e.source_event_id == "evt_g1" for e in ents)


def test_failure_event_extracts_failure_entity_and_claim():
    ev = _event(
        "skill_failure",
        {
            "episode_id": "ep1",
            "dispatch_id": "d1",
            "skill_name": "x",
            "failure_class": "test_failed",
            "kind": "skill",
            "context": "tests failed",
        },
    )
    ents, claims = X.extract_from_event(ev)
    assert any(e.entity_kind == "failure" and e.canonical_name == "test_failed" for e in ents)
    assert any(c.predicate == "failed_because" and c.object == "test_failed" for c in claims)


# ── claims ───────────────────────────────────────────────────────────────────--
def test_skill_result_extracts_status_tool_file_test_claims():
    _, claims = X.extract_from_event(_skill_result_event())
    triples = {(c.subject, c.predicate, c.object) for c in claims}
    assert ("omega-planner-executor", "produced_status", "success") in triples
    assert ("omega-planner-executor", "used_tool", "read_file") in triples
    assert ("omega-planner-executor", "changed_file", "schema/x.py") in triples
    assert ("d1", "verified_by", "tests/test_x.py") in triples
    assert all(isinstance(c, MemoryClaim) and c.validate() == [] for c in claims)
    assert all(c.evidence_event_ids == ("evt_g1",) for c in claims)
    assert all(c.confidence == 1.0 for c in claims)


def test_skill_selection_extracts_selected_for_claim():
    ev = _event(
        "skill_execution_pack",
        {
            "skill_name": "codex-grade-coding",
            "skill_id": "5.01.qwenskill-codex-grade-coding",
            "execution_kind": "INSPECT",
            "dispatch_id": "d1",
            "objective": "inspect the dispatch schema",
        },
    )
    _, claims = X.extract_from_event(ev)
    triples = {(c.subject, c.predicate, c.object) for c in claims}
    assert ("codex-grade-coding", "selected_for", "inspect the dispatch schema") in triples


def test_global_b_extracts_satisfaction_and_continuation_claims():
    ev = _event(
        "global_b_skill_update",
        {
            "episode_id": "ep1",
            "dispatch_id": "d1",
            "intent_satisfied": False,
            "objective_complete": False,
            "more_work_needed": True,
            "continuation_signals": ["verification_needed"],
            "continuation_pressure": {"reason": "verification_needed", "objective": "run tests"},
        },
    )
    _, claims = X.extract_from_event(ev)
    triples = {(c.subject, c.predicate, c.object) for c in claims}
    assert ("ep1", "intent_satisfied", "false") in triples
    assert ("ep1", "created_pressure", "verification_needed") in triples


def test_capability_event_extracts_capability_signal_claim():
    ev = _event(
        "capability_reinforced",
        {"signal": "reinforce", "skill_name": "x", "skill_id": "5.01.qwenskill-x", "delta": 0.05},
    )
    _, claims = X.extract_from_event(ev)
    assert any(c.predicate == "capability_signal" and c.object == "reinforce" for c in claims)


# ── determinism + idempotency ────────────────────────────────────────────────--
def test_extraction_is_deterministic():
    ev = _skill_result_event()
    e1, c1 = X.extract_from_event(ev)
    e2, c2 = X.extract_from_event(ev)
    assert [e.entity_id for e in e1] == [e.entity_id for e in e2]
    assert [c.claim_id for c in c1] == [c.claim_id for c in c2]


def test_extract_from_events_dedupes_by_id():
    # two events naming the same skill -> the skill entity appears once
    e1 = _skill_result_event()
    e2 = _event(
        "skill_execution_pack",
        {
            "skill_name": "omega-planner-executor",
            "skill_id": "5.01.qwenskill-omega-planner-executor",
            "execution_kind": "PATCH",
            "dispatch_id": "d1",
            "objective": "do it",
        },
        eid="evt_g2",
    )
    ents, claims = X.extract_from_events([e1, e2])
    skill_ents = [
        e for e in ents if e.entity_kind == "skill" and e.canonical_name == "omega-planner-executor"
    ]
    assert len(skill_ents) == 1
    # ids are unique across the deduped set
    assert len({e.entity_id for e in ents}) == len(ents)
    assert len({c.claim_id for c in claims}) == len(claims)


def test_extraction_is_model_free():
    # the extract module must not IMPORT any model/inference/runtime surface.
    # (scan import lines only — the dispatch table legitimately names event kinds
    # like "qwen_skill_result", which are cascade phase labels, not model calls.)
    import inspect
    import memory.taxonomy.extract as mod

    import_lines = [
        ln
        for ln in inspect.getsource(mod).splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    blob = "\n".join(import_lines).lower()
    for banned in ("metacog", "qwen", "runtime", "llm", "openai", "model_infer", "inference"):
        assert banned not in blob, banned
    # and it depends only on stdlib + the taxonomy package
    allowed = ("memory.taxonomy", "typing", "__future__")
    assert all(any(a in ln for a in allowed) for ln in import_lines), import_lines
