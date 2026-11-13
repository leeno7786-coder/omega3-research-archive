"""Cascade audit 2026-07-09 — _packet_from_episode must round-trip EVERY packet field.

The production Omega pass never sees the OmegaEpisodeInput that attach_omega_packet
builds — that object is discarded and run_omega_for_episode rebuilds the packet from
episode.global_['omega_packet'] via _packet_from_episode. If that hand-written
reconstruction omits a field, the field silently collapses to its dataclass default
and the prompt block that renders it goes dark (this is how foldback_packet,
learning_map, mastery_agenda, execution_experience, ask_from_concern and
rollback_events were being dropped). This test pins the reconstruction against re-rot:
it forces every contract field to a NON-default value and asserts each survives the
to_dict() -> _packet_from_episode round-trip.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from omega_loop.episode.run import _packet_from_episode
from omega_loop.episode.types import MasteryView, OmegaEpisodeInput, bounded_browser_run

# Fields _packet_from_episode intentionally does NOT reconstruct because they are
# populated by a dedicated injector, not by packet serialization:
#   current_identity/candidate_identity -> _with_current_identity (chat reflection only).
#   cross_lane_mail -> held_omega_mail: recomputed from mail-store OWNERSHIP on every read,
#     because a saved packet is no proof the episode still holds the mail it once bound.
#     Trusting the serialized rows let the prompt render mail another episode had taken.
#   omitted_required_mail_ids -> omitted_required_omega_mail: the live binding record,
#     not a saved packet field.
#   lane_entry_block -> the window episode's live LaneEntry (T9): like bound mail, what a
#     saved packet says was claimed is no proof it is still this episode's to be shown.
_INJECTED_ELSEWHERE = {"current_identity", "candidate_identity", "cross_lane_mail",
                       "lane_entry_block", "omitted_required_mail_ids"}


def test_browser_run_contract_retains_an_operator_stop():
    receipt = bounded_browser_run({
        "status": "stopped",
        "stopped_reason": "operator requested stop",
        "final_page": {},
    })

    assert receipt["status"] == "stopped"
    assert receipt["stopped_reason"] == "operator requested stop"


def _fully_populated_packet() -> OmegaEpisodeInput:
    """Every field set to a distinctive non-default value.

    When a new field is added to OmegaEpisodeInput, the non-default guard in the test
    below fails until it is added here — which in turn forces the round-trip assertion
    to cover it, so a new field cannot be silently dropped by _packet_from_episode.
    """
    return OmegaEpisodeInput(
        omega_lane="browser",
        original_objective="Browse models",
        cross_lane_mail=({"mail_id": "m-rt", "purpose": "operator_instruction",
                          "requirement": "required", "source_actor": "operator",
                          "source_episode_id": "ep-op", "arrival_seq": 7,
                          "payload": {"text": "use sqlite"}, "provenance": ["ep-op"]},),
        browser_perception={"url": "https://example.org/models", "title": "Models"},
        browser_run={"status": "done", "steps": 1, "actions": [
            {"step": 1, "action": "click", "summary": "click (10,10)", "ok": True, "error": "", "url": ""}],
            "final_page": {"url": "https://example.org/model", "title": "Model", "text": "A model"},
            "summary": "Opened the model.", "question": "", "stopped_reason": "", "operator_notes_seen": 0},
        episode_id="ep-rt",
        intent="LEARN",
        action="USE_TOOL",
        tool="web_fetch",
        domain="quantum_physics",
        operator_text="what is a qubit?",
        outcome="failure",
        failure_reason="boom",
        raw_tool_result={"results": [1, 2]},
        recall=({"kind": "remembered_knowledge", "confidence": 0.8},),
        recall_error="read_memory timeout",
        prior_mastery=MasteryView(
            domain="quantum_physics",
            knowledge_mastery=0.2,
            application_mastery=0.1,
            target_mastery=0.7,
        ),
        neuromod_state={
            "dopamine": -0.2,
            "serotonin": 0.15,
            "stress": 0.4,
            "spike": 0.0,
        },
        applied={"status": "success", "summary": "did"},
        execution_experience=(
            {"domain": "python", "attempts": 2, "successes": 1, "last_outcome": "success"},
        ),
        learning_map={
            "topics": [{"domain": "python", "knowledge": 0.1}],
            "total": 1,
            "learned_count": 1,
            "empty_count": 0,
            "mastered_count": 0,
        },
        prior_builds=({"skill_name": "build_x", "objective": "ship x"},),
        build_in_progress=({"objective": "y", "progress_digest": "half"},),
        open_questions=({"question": "which db?", "windows_survived": 1},),
        rollback_events=({"type": "dev_rollback", "snapshot_id": "snap-1"},),
        foldback_packet={"pressure_surface": {"current_pressure": {}}, "mastery_delta": {}},
        response_text="a qubit is ...",
        motor_program={
            "program_id": "mp-rt",
            "parent_program_id": "",
            "outcome_id": "out-rt",
            "episode_id": "ep-rt",
            "attempt_number": 1,
            "created_at": "2026-07-11T00:00:00+00:00",
            "outcome_contract": {
                "objective": "verify round trip",
                "acceptance_criteria": ["packet survives"],
            },
            "pathway": {
                "pathway_id": "round-trip",
                "revision": 1,
                "name": "round trip",
                "method": "serialize and rebuild",
                "operational_rationale": "pins continuity",
            },
            "execution_plan": {
                "capability_class": "testing",
                "operations": ["round trip packet"],
            },
            "evidence_contract": {
                "required_checks": ["packet round trip check"],
                "completion_signals": ["equal"],
            },
        },
        originating_pressure={
            "pressure_id": "p-rt",
            "name": "round_trip_gap",
            "source_episode_id": "ep-before-rt",
            "outcome_id": "out-rt",
            "desired_state": {"packet": "complete"},
            "observed_state": {"packet": "unverified"},
            "gap_description": "packet continuity needs verification",
            "evidence": ["round trip not yet run"],
            "failed_program_ids": ["mp-old"],
            "urgency": 0.7,
            "persistence": 1,
            "proposed_action": {
                "intended_transformation": "verify packet continuity",
                "candidate_pathway": "round-trip",
                "required_capabilities": ["testing"],
            },
            "activation_conditions": ["packet unverified"],
            "resolution_conditions": ["packet fields equal"],
            "supersedes_pressure_id": "",
        },
        current_identity=({"trait": "reach_for_structure", "pattern": "I structure"},),
        candidate_identity=({"trait": "try_an_analogy", "pattern": "one observation"},),
        fetched_knowledge={
            "kind": "world",
            "sources": [{"name": "Wikipedia — Qubit", "url": "https://example.org/qubit"}],
            "learned": "a qubit superposes basis states",
            "receipt": {"coord": [2, 0, 0], "status": "committed"},
        },
        proactive_policy={
            "policy_id": "policy-rt",
            "thread_id": "thread-rt",
            "operator_text": "practice ten scenarios",
        },
        proactive_dispatch={
            "presentation_kind": "PRESENT_RESULT",
            "proactive_application_id": "app-rt",
        },
        execution_foldback={
            "schema_version": "omega.execution_foldback.v1",
            "episode_id": "ep-rt",
            "request": {
                "origin": "operator_query",
                "text": "what is a qubit?",
                "objective": "verify round trip",
            },
            "motor": {
                "program_id": "mp-rt",
                "outcome_id": "out-rt",
                "acceptance_criteria": ["packet survives"],
            },
            "dispatch": {
                "episode_id": "ep-rt",
                "dispatch_id": "dispatch-rt",
                "skill_id": "skill-rt",
                "skill_name": "packet-check",
                "status": "failure",
            },
            "execution_evidence": {
                "program_id": "mp-rt",
                "episode_id": "ep-rt",
                "status": "failure",
                "blockers": ["round trip not yet verified"],
                "progress_digest": "packet assembled",
            },
            "verification": {
                "all_green": False,
                "checks": [{"name": "packet_round_trip", "ok": False}],
            },
            "verdict": {
                "satisfied": False,
                "more_work_needed": True,
                "blockers": ["round trip not yet verified"],
                "summary": "verification remains",
            },
        },
        onboarding_context={
            "occupation": "software_engineer",
            "operator_id": "operator",
            "categories": [
                {
                    "title": "Python",
                    "lobe": 8,
                    "y": 3,
                    "dirname": "python",
                    "memory_id": "8.03.operator-work-topic.python",
                }
            ],
            "source_citation": {
                "source_id": "src-onboarding-rt",
                "source_type": "operator_onboarding",
            },
        },
    )


def _fully_populated_window_packet() -> OmegaEpisodeInput:
    # Lane evidence is legal only on its own lane, so no single packet can carry both
    # browser evidence and a window view: the window lane's field is populated here.
    return dataclasses.replace(
        _fully_populated_packet(),
        omega_lane="window",
        browser_perception=None,
        browser_run=None,
        window_context={"window_id": "win-rt", "episode_count": 1,
                        "episodes": [{"n": 1, "episode_id": "ep-0", "intent": "LEARN"}]},
    )


def _fully_populated_agent_packet() -> OmegaEpisodeInput:
    # The agent lane's own evidence: one terminal-agent batch receipt, legal only on this lane.
    return dataclasses.replace(
        _fully_populated_packet(),
        omega_lane="agent",
        browser_perception=None,
        browser_run=None,
        agent_run={
            "status": "job_claimed",
            "steps": 1,
            "job_id": "job-rt",
            "project_root": "E:\todo_app",
            "actions": [{"step": 1, "call_id": "call-0", "tool": "start_job", "args": "{}",
                         "ok": True, "summary": "project owned", "error": ""}],
            "touched_files": [],
            "project_files": ["app.py"],
            "summary": "claimed the project",
            "question": "",
            "stopped_reason": "",
        },
    )


def _field_default(f: dataclasses.Field):
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return dataclasses.MISSING  # required field — no default to rot to


def test_fixture_sets_every_reconstructed_field_to_non_default():
    """Meaningfulness guard: a new contract field forces a fixture update here, so the
    round-trip test below can never trivially pass by leaving the new field at default."""
    packets = (_fully_populated_packet(), _fully_populated_window_packet(),
               _fully_populated_agent_packet())
    for f in dataclasses.fields(OmegaEpisodeInput):
        if f.name in _INJECTED_ELSEWHERE:
            continue
        default = _field_default(f)
        assert any(getattr(packet, f.name) != default for packet in packets), (
            f"_fully_populated_packet() must set a NON-default value for '{f.name}' so the "
            f"round-trip assertion is meaningful — add it to the fixture."
        )


@pytest.mark.parametrize("fixture", [_fully_populated_packet, _fully_populated_window_packet,
                                     _fully_populated_agent_packet])
def test_packet_from_episode_round_trips_all_serialized_fields(fixture):
    original = fixture()
    episode = SimpleNamespace(global_={"omega_packet": original.to_dict()})

    rebuilt = _packet_from_episode(episode)

    assert rebuilt is not None
    for f in dataclasses.fields(OmegaEpisodeInput):
        if f.name in _INJECTED_ELSEWHERE:
            continue
        assert getattr(rebuilt, f.name) == getattr(original, f.name), (
            f"_packet_from_episode dropped or altered '{f.name}' on the "
            f"omega_packet round-trip — the mind would reason on a hollow packet."
        )


def test_missing_omega_packet_returns_none():
    assert _packet_from_episode(SimpleNamespace(global_={})) is None
