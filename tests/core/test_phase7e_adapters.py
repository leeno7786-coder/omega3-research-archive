"""Phase 7E — cascade event adapters.

Pure functions: a closed episode's slots -> MemoryEvents. Adapters never mutate the
episode and never touch the graph or ledger. Event payloads must preserve enough
evidence to rebuild the graph (esp. the qwen skill result's tool_calls/files_changed/
tests_run/diff_summary/failure_class/guard_status).
"""

from __future__ import annotations

import copy

from memory.taxonomy import adapters as A
from memory.taxonomy.schemas import MemoryEvent


CREATED = "2026-06-21T12:00:00Z"


def _skill_episode():
    return {
        "episode_id": "ep_skill_1",
        "opened_by": "operator_command",
        "closed_at": CREATED,
        "cascade_status": "completed",
        "pressure": {
            "aggregate": 1.0,
            "opened_by": "operator_command",
            "contributors": [
                {
                    "id": "operator_command",
                    "weight": 1.0,
                    "detail": {"message": "inspect the dispatch schema"},
                }
            ],
            "trajectory_snapshot": None,
        },
        "global": {
            "intent_decision": {
                "episode_id": "ep_skill_1",
                "intent": "USE_SKILL",
                "confidence": 0.9,
                "reason": "agentic",
            },
            "outcome": {"summary": "inspected", "grounded": True, "followups": []},
            "skill_outcome": {
                "episode_id": "ep_skill_1",
                "dispatch_id": "dispatch_abc",
                "intent_satisfied": False,
                "objective_complete": False,
                "more_work_needed": True,
                "continuation_signals": ["verification_needed"],
                "continuation_pressure": {
                    "kind": "execution_repair",
                    "reason": "verification_needed",
                    "objective": "run tests",
                },
                "close_episode": False,
                "ask_operator": False,
            },
            "omega_result": {"cognized": True},
        },
        "meta": {
            "execution_action_pack": {
                "action": "USE_SKILL",
                "tool": "",
                "execution_inputs": {"objective": "inspect the dispatch schema"},
            },
            "skill_execution_pack": {
                "episode_id": "ep_skill_1",
                "skill_name": "omega-planner-executor",
                "skill_id": "5.01.qwenskill-omega-planner-executor",
                "execution_kind": "INSPECT",
                "dispatch_id": "dispatch_abc",
                "allowed_tools": ["read_file"],
                "forbidden_tools": ["delete_file"],
            },
            "reflection": {"status": "valid", "disposition": "executed"},
            "skill_reflection": {
                "episode_id": "ep_skill_1",
                "dispatch_id": "dispatch_abc",
                "outcome_kind": "skill_success",
                "failure_class": None,
                "satisfied_dispatch": True,
                "guard_status": {"status": "allowed"},
            },
            "skill_consolidation": {
                "record": {"skill_name": "omega-planner-executor"},
                "applied": {"reinforced": True, "new_strength": 0.55},
                "summary": {
                    "selected_skill": "omega-planner-executor",
                    "why": "best match",
                    "outcome": "success",
                },
            },
        },
        "auto": {
            "status": "success",
            "action": "USE_SKILL",
            "failure_kind": "",
            "failure_reason": "",
            "outputs": {
                "skill_result": {
                    "episode_id": "ep_skill_1",
                    "dispatch_id": "dispatch_abc",
                    "skill_name": "omega-planner-executor",
                    "skill_id": "5.01.qwenskill-omega-planner-executor",
                    "execution_kind": "INSPECT",
                    "status": "success",
                    "tool_calls": [{"tool_name": "read_file"}],
                    "files_changed": ["schema/x.py"],
                    "commands_run": [],
                    "tests_run": ["tests/test_x.py"],
                    "diff_summary": {"git_diff_present": True, "git_diff": "@@"},
                    "failure_class": None,
                    "guard_status": {"status": "allowed"},
                    "evidence": ["read schema/x.py"],
                    "unresolved_blockers": [],
                },
                "skill_dispatch": {"dispatch_id": "dispatch_abc"},
                "skill_status": "success",
                "skill_objective": "inspect the dispatch schema",
                "skill_guard_status": {"status": "allowed"},
            },
        },
    }


# ── individual adapters ─────────────────────────────────────────────────────────
def test_pressure_ignition_event():
    e = A.event_from_pressure_ignition(_skill_episode(), CREATED)
    assert e.event_kind == "pressure_ignition"
    assert e.episode_id == "ep_skill_1"
    assert e.payload["opened_by"] == "operator_command"
    assert e.validate() == []


def test_global_a_event_carries_intent():
    e = A.event_from_global_a(_skill_episode(), CREATED)
    assert e.event_kind == "global_a_intent"
    assert e.payload["intent"] == "USE_SKILL"
    assert e.validate() == []


def test_meta_a_pack_event():
    e = A.event_from_meta_a_pack(_skill_episode(), CREATED)
    assert e.event_kind == "meta_a_action_pack"
    assert e.payload["action"] == "USE_SKILL"


def test_skill_execution_pack_event_preserves_selection():
    e = A.event_from_skill_execution_pack(_skill_episode(), CREATED)
    assert e.event_kind == "skill_execution_pack"
    for k in ("skill_name", "skill_id", "execution_kind", "dispatch_id", "allowed_tools"):
        assert k in e.payload


def test_qwen_skill_result_event_preserves_all_evidence():
    e = A.event_from_qwen_skill_result(_skill_episode(), CREATED)
    assert e.event_kind == "qwen_skill_result"
    for k in (
        "status",
        "tool_calls",
        "files_changed",
        "commands_run",
        "tests_run",
        "diff_summary",
        "failure_class",
        "guard_status",
        "skill_name",
        "skill_id",
        "execution_kind",
        "dispatch_id",
    ):
        assert k in e.payload, k
    assert e.payload["files_changed"] == ["schema/x.py"]
    assert e.payload["tests_run"] == ["tests/test_x.py"]


def test_qwen_skill_result_reads_meta_snapshot_when_outputs_cleared():
    # store.close() clears the live auto.outputs before the capture hook runs, so the foldback
    # snapshots the normalized result into meta.skill_result_snapshot. The adapter MUST fall
    # back to it — otherwise no qwen_skill_result event is written and the whole workflow_memory
    # / recall chain never fires (the build leaves no artifact memory; operator 2026-06-25).
    ep = _skill_episode()
    snap = ep["auto"]["outputs"]["skill_result"]
    ep["auto"]["outputs"] = {}  # outputs cleared by store.close()
    ep["meta"]["skill_result_snapshot"] = snap  # foldback's snapshot survives close
    e = A.event_from_qwen_skill_result(ep, CREATED)
    assert e is not None and e.event_kind == "qwen_skill_result"
    assert e.payload["skill_name"] == "omega-planner-executor"
    assert e.payload["files_changed"] == ["schema/x.py"]


def test_auto_outcome_event():
    e = A.event_from_auto_outcome(_skill_episode(), CREATED)
    assert e.event_kind == "auto_outcome"
    assert e.payload["status"] == "success"


def test_meta_b_and_skill_meta_b_events():
    assert A.event_from_meta_b(_skill_episode(), CREATED).event_kind == "meta_b_outcome"
    sk = A.event_from_skill_meta_b(_skill_episode(), CREATED)
    assert sk.event_kind == "meta_b_skill_outcome"
    assert sk.payload["outcome_kind"] == "skill_success"


def test_global_b_and_skill_global_b_events():
    assert A.event_from_global_b(_skill_episode(), CREATED).event_kind == "global_b_update"
    sg = A.event_from_skill_global_b(_skill_episode(), CREATED)
    assert sg.event_kind == "global_b_skill_update"
    assert sg.payload["continuation_signals"] == ["verification_needed"]


def test_skill_consolidation_event():
    e = A.event_from_skill_consolidation(_skill_episode(), CREATED)
    assert e.event_kind == "deepsleep_skill_summary"
    assert e.payload["summary"]["selected_skill"] == "omega-planner-executor"


def test_adapter_returns_none_on_absent_slot():
    bare = {"episode_id": "x", "closed_at": CREATED}
    assert A.event_from_skill_execution_pack(bare, CREATED) is None
    assert A.event_from_qwen_skill_result(bare, CREATED) is None
    assert A.event_from_skill_global_b(bare, CREATED) is None


# ── record / query / packet adapters ────────────────────────────────────────────
def test_capability_signal_event_kind_follows_signal():
    reinforce = {
        "signal": "reinforce",
        "skill_name": "x",
        "skill_id": "5.01.qwenskill-x",
        "delta": 0.05,
    }
    weaken = {
        "signal": "weaken_skill",
        "skill_name": "x",
        "skill_id": "5.01.qwenskill-x",
        "delta": -0.05,
    }
    assert (
        A.event_from_capability_signal(reinforce, episode_id="ep1", created_at=CREATED).event_kind
        == "capability_reinforced"
    )
    assert (
        A.event_from_capability_signal(weaken, episode_id="ep1", created_at=CREATED).event_kind
        == "capability_weakened"
    )


def test_failure_record_event_kind_follows_kind():
    cases = {
        "tool": "tool_failure",
        "skill": "skill_failure",
        "bridge_unavailable": "bridge_failure",
        "safety": "safety_block",
    }
    for kind, expected in cases.items():
        rec = {"kind": kind, "context": "ctx", "failure_class": kind}
        e = A.event_from_failure_record(rec, episode_id="ep1", created_at=CREATED)
        assert e.event_kind == expected, (kind, e.event_kind)


def test_recall_query_and_result_events():
    q = {"query": "what skill inspected", "query_kind": "skill_use", "query_hash": "qh1"}
    p = {
        "query": "what skill inspected",
        "query_hash": "qh1",
        "snapshot_hash": "sh1",
        "results": [{"node_id": "n1"}],
        "evidence_event_ids": ["evt_abc"],
    }
    assert (
        A.event_from_recall_query(q, episode_id="ep1", created_at=CREATED).event_kind
        == "recall_query"
    )
    assert (
        A.event_from_recall_result(p, episode_id="ep1", created_at=CREATED).event_kind
        == "recall_result"
    )


# ── aggregate + invariants ──────────────────────────────────────────────────────
def test_events_from_episode_full_skill_cycle():
    events = A.events_from_episode(_skill_episode(), CREATED)
    kinds = {e.event_kind for e in events}
    expected = {
        "pressure_ignition",
        "global_a_intent",
        "meta_a_action_pack",
        "skill_execution_pack",
        "qwen_skill_result",
        "auto_outcome",
        "meta_b_outcome",
        "meta_b_skill_outcome",
        "global_b_update",
        "global_b_skill_update",
        "deepsleep_skill_summary",
    }
    assert expected <= kinds
    assert all(isinstance(e, MemoryEvent) for e in events)
    assert all(e.validate() == [] for e in events)


def test_events_from_episode_is_deterministic():
    ep = _skill_episode()
    ids1 = [e.event_id for e in A.events_from_episode(ep, CREATED)]
    ids2 = [e.event_id for e in A.events_from_episode(ep, CREATED)]
    assert ids1 == ids2


def test_adapters_do_not_mutate_episode():
    ep = _skill_episode()
    before = copy.deepcopy(ep)
    A.events_from_episode(ep, CREATED)
    assert ep == before


def test_failed_episode_still_produces_events():
    ep = _skill_episode()
    ep["auto"]["status"] = "failure"
    ep["auto"]["outputs"]["skill_result"]["status"] = "failure"
    ep["auto"]["outputs"]["skill_result"]["failure_class"] = "test_failed"
    ep["meta"]["skill_reflection"]["outcome_kind"] = "skill_failure"
    events = A.events_from_episode(ep, CREATED)
    qr = [e for e in events if e.event_kind == "qwen_skill_result"][0]
    assert qr.payload["status"] == "failure"
    assert qr.payload["failure_class"] == "test_failed"
