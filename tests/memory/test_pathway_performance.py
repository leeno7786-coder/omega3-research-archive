"""Canonical pathway outcomes and their disposable bounded projection."""

from __future__ import annotations

import json

from memory import episode_sram, pathway_performance
from memory.pathway_performance import (
    rebuild_pathway_projection,
    recent_pathway_outcomes,
    record_pathway_outcome,
)
from schema.pathway_outcome import PathwayOutcome


def _outcome(program_id: str = "mp1", *, verified: bool = True) -> PathwayOutcome:
    pre = {"knowledge": 0.2, "application": 0.1}
    post = {"knowledge": 0.4, "application": 0.2} if verified else pre
    return PathwayOutcome.from_dict(
        {
            "pathway_id": "derive-implement-verify",
            "pathway_method": "derive implement and verify",
            "material_signature": {
                "capability_class": "code_execution",
                "operations": ["derive", "implement", "verify"],
            },
            "revision": 1,
            "task_context": "learn backpropagation",
            "domain": "machine-learning",
            "program_id": program_id,
            "episode_id": f"ep-{program_id}",
            "outcome_id": f"out-{program_id}",
            "pre_mastery": pre,
            "post_mastery": post,
            "knowledge_delta": post["knowledge"] - pre["knowledge"],
            "application_delta": post["application"] - pre["application"],
            "verification_verdict": "verified" if verified else "unverified",
            "progress_efficiency": 0.3 if verified else 0.0,
            "failure_fingerprint": "" if verified else "failed-check",
            "neuromodulator_response": {
                "dopamine": 0.6 if verified else -0.4,
                "plasticity": 0.8,
                "stress": 0.0 if verified else 0.3,
                "serotonin": 0.2,
                "noradrenaline": 0.24,
            },
        }
    )


def test_record_enters_sram_without_projecting_ahead_of_truth(tmp_path, monkeypatch):
    projection = tmp_path / "pathway_performance.json"
    monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", projection)
    episode_sram.reset()
    episode_sram.open_bucket("ep-mp1", pipeline_id=0)
    try:
        record_pathway_outcome(_outcome())
        item = episode_sram.pending()[0]
    finally:
        episode_sram.reset()

    envelope, kwargs = item.envelope, item.write_options
    assert envelope.type == "pathway_outcome"
    assert envelope.payload["enrichment"]["episode_id"] == "ep-mp1"
    assert envelope.payload["enrichment"]["outcome_id"] == "out-mp1"
    assert kwargs == {"propose": False, "bypass_guardian": True, "skip_embed": False}
    assert not projection.exists()


def test_canonical_summary_states_unverified_truth(tmp_path, monkeypatch):
    projection = tmp_path / "pathway_performance.json"
    monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", projection)
    episode_sram.reset()
    episode_sram.open_bucket("ep-mp1", pipeline_id=0)
    try:
        record_pathway_outcome(_outcome(verified=False))
        envelope = episode_sram.pending()[0].envelope
    finally:
        episode_sram.reset()

    assert "unverified" in envelope.payload["summary"].lower()


def test_projection_is_bounded_and_recent_query_filters_episode_ids(tmp_path, monkeypatch):
    projection = tmp_path / "pathway_performance.json"
    monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", projection)
    rows = []
    for index in range(55):
        row = _outcome(f"mp{index}").to_dict()
        row["episode_id"] = f"ep{index}"
        row["expected_success"] = 0.4
        rows.append(row)

    rebuild_pathway_projection(rows)

    recent = recent_pathway_outcomes(cap=100)
    assert len(recent) == 40
    assert recent[0]["episode_id"] == "ep15"
    assert [r["episode_id"] for r in recent_pathway_outcomes(("ep18", "ep42"))] == ["ep18", "ep42"]


def test_projection_rebuild_is_deterministic_after_deletion(tmp_path, monkeypatch):
    projection = tmp_path / "pathway_performance.json"
    monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", projection)
    assessment1 = {
        "type": "motor_attempt_assessment",
        "text": json.dumps(
            {
                "program_id": "mp1",
                "episode_id": "ep1",
                "outcome_id": "out-mp1",
                "assessment": {"expected_success": 0.2},
            }
        ),
    }
    assessment2 = {
        "type": "motor_attempt_assessment",
        "text": json.dumps(
            {
                "program_id": "mp2",
                "episode_id": "ep2",
                "outcome_id": "out-mp2",
                "assessment": {"expected_success": 0.6},
            }
        ),
    }
    rows = [
        assessment2,
        {"type": "pathway_outcome", "text": json.dumps(_outcome("mp2", verified=False).to_dict())},
        assessment1,
        {"type": "pathway_outcome", "text": json.dumps(_outcome("mp1").to_dict())},
    ]

    first = rebuild_pathway_projection(rows)
    first_bytes = projection.read_bytes()
    projection.unlink()
    second = rebuild_pathway_projection(rows)

    assert second == first
    assert projection.read_bytes() == first_bytes
    pathway = second["pathways"]["derive-implement-verify"]
    assert pathway["attempts"] == 2
    assert pathway["verified_successes"] == 1
    assert pathway["failure_fingerprints"] == ["failed-check"]
    assert pathway["expected_success"] == 0.4
    assert second["unmatched_assessments"] == []


def test_projection_rebuild_rehydrates_canonical_window_decisions(tmp_path, monkeypatch):
    projection = tmp_path / "pathway_performance.json"
    monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", projection)
    outcome = {"type": "pathway_outcome", "text": json.dumps(_outcome("mp1").to_dict())}
    older = {
        "type": "pathway_window_update",
        "timestamp_created": "2026-07-11T01:00:00Z",
        "text": json.dumps(
            {
                "window_id": "window-1",
                "pathway_id": "derive-implement-verify",
                "decision": "revise",
                "reason": "first revision",
                "candidate_method": "derive then verify",
                "evidence_program_ids": ["mp1"],
                "window_order": {"kind": "timestamp", "value": "2026-07-11T01:00:00Z"},
                "source_tie_break": "older-source",
            }
        ),
    }
    latest = {
        "type": "pathway_window_update",
        "timestamp_created": "2026-07-11T02:00:00Z",
        "text": json.dumps(
            {
                "window_id": "window-2",
                "pathway_id": "derive-implement-verify",
                "decision": "retain",
                "reason": "verified again",
                "candidate_method": "",
                "evidence_program_ids": ["mp1"],
                "window_order": {"kind": "timestamp", "value": "2026-07-11T02:00:00Z"},
                "source_tie_break": "latest-source",
            }
        ),
    }
    sources = [latest, outcome, older, dict(latest)]

    first = rebuild_pathway_projection(sources)
    first_bytes = projection.read_bytes()
    projection.unlink()
    second = rebuild_pathway_projection(list(reversed(sources)))

    assert second == first
    assert projection.read_bytes() == first_bytes
    assert [row["window_id"] for row in second["window_updates"]] == [
        "window-1",
        "window-2",
    ]
    assert second["pathways"]["derive-implement-verify"]["window_decision"] == {
        "window_id": "window-2",
        "pathway_id": "derive-implement-verify",
        "decision": "retain",
        "reason": "verified again",
        "candidate_method": "",
        "evidence_program_ids": ["mp1"],
        "timestamp": "2026-07-11T02:00:00Z",
        "window_order": {"kind": "timestamp", "value": "2026-07-11T02:00:00Z"},
        "source_tie_break": "latest-source",
    }


def test_rebuild_without_rows_loads_canonical_index_records(tmp_path, monkeypatch):
    projection = tmp_path / "pathway_performance.json"
    monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", projection)
    assessment = {
        "type": "motor_attempt_assessment",
        "text": json.dumps(
            {
                "program_id": "mp1",
                "episode_id": "ep-mp1",
                "outcome_id": "out-mp1",
                "assessment": {"expected_success": 0.25},
            }
        ),
    }
    outcome = {
        "type": "pathway_outcome",
        "text": json.dumps(_outcome("mp1").to_dict()),
    }
    records = {"assessment.json": assessment, "outcome.json": outcome}
    from memory.engine import walker

    monkeypatch.setattr(walker, "load_coord_index", lambda: None)
    monkeypatch.setattr(
        walker,
        "coord_index_snapshot",
        lambda: {
            "4.06.a": {"type": "motor_attempt_assessment", "path": "assessment.json"},
            "4.06.b": {"type": "pathway_outcome", "path": "outcome.json"},
        },
    )
    monkeypatch.setattr(walker, "load_meta_by_path", lambda path: records[path])

    rebuilt = rebuild_pathway_projection()

    assert rebuilt["pathways"]["derive-implement-verify"]["attempts"] == 1
    assert rebuilt["pathways"]["derive-implement-verify"]["expected_success"] == 0.25


def test_canonical_scan_rehydrates_only_reasoning_heuristic_window_updates(tmp_path, monkeypatch):
    projection = tmp_path / "pathway_performance.json"
    monkeypatch.setattr(pathway_performance, "PATHWAY_PROJECTION_PATH", projection)
    outcome = {"type": "pathway_outcome", "text": json.dumps(_outcome("mp1").to_dict())}
    update = {
        "type": "pathway_window_update",
        "timestamp_created": "2026-07-11T02:00:00Z",
        "text": json.dumps(
            {
                "window_id": "window-1",
                "pathway_id": "derive-implement-verify",
                "decision": "retain",
                "reason": "verified",
                "candidate_method": "",
                "evidence_program_ids": ["mp1"],
                "window_order": {"kind": "timestamp", "value": "2026-07-11T02:00:00Z"},
                "source_tie_break": "good-source",
            }
        ),
    }
    records = {
        "outcome.json": outcome,
        "good-update.json": update,
        "wrong-lobe.json": {**update, "timestamp_created": "2026-07-11T03:00:00Z"},
    }
    from memory.engine import walker

    monkeypatch.setattr(walker, "load_coord_index", lambda: None)
    monkeypatch.setattr(
        walker,
        "coord_index_snapshot",
        lambda: {
            "4.06.a": {
                "type": "pathway_outcome",
                "coord": [4, 6, 1],
                "path": "outcome.json",
            },
            "3.03.a": {
                "type": "pathway_window_update",
                "coord": [3, 3, 0],
                "path": "good-update.json",
            },
            "4.06.poison": {
                "type": "pathway_window_update",
                "coord": [4, 6, 1],
                "path": "wrong-lobe.json",
            },
        },
    )
    monkeypatch.setattr(walker, "load_meta_by_path", lambda path: records[path])

    rebuilt = rebuild_pathway_projection()

    assert [row["timestamp"] for row in rebuilt["window_updates"]] == ["2026-07-11T02:00:00Z"]
