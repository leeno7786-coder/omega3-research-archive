"""WO-02 — the sealed EpisodeRecord: ten blocks, five independent outcome axes, coverage.

The axes are the point. Today the live episode collapses them into ONE field —
`GlobalOutcomePack.outcome["status"]` plus a `cascade_status` on the carrier — so
"the cascade finished" and "the task worked" are the same sentence. WO-01 measured the
cost: a window folded over ten episodes of a build going round in a circle and called
them "hollow and indistinguishable". These tests make the collapse impossible to
re-express.
"""

from __future__ import annotations

import pytest

from schema.continuity_json import (
    CanonicalRef,
    ContinuityContractError,
    Observation,
    canonical_bytes,
    parse_json_object,
)
from schema.episode_record import (
    ACTION_STATUSES,
    CASCADE_STATUSES,
    EPISODE_RECORD_BLOCKS,
    FEEDBACK_STATUSES,
    MEMORY_COMMIT_STATUSES,
    TASK_OUTCOMES,
    VERIFICATION_STATUSES,
    EpisodeOutcome,
    EpisodeRecord,
)
from tests.helpers.continuity_records import make_episode_record, make_event


# ── the ten blocks ───────────────────────────────────────────────────────────


def test_the_record_declares_exactly_ten_required_blocks():
    assert EPISODE_RECORD_BLOCKS == (
        "schema_version",
        "identity",
        "temporal",
        "origin",
        "objective",
        "events",
        "outcome",
        "metacognition",
        "learning",
        "coverage",
    )


def test_a_sealed_record_carries_every_block():
    as_dict = make_episode_record().to_dict()
    assert tuple(sorted(as_dict)) == tuple(sorted(EPISODE_RECORD_BLOCKS))


def test_a_record_missing_a_block_is_rejected():
    for block in EPISODE_RECORD_BLOCKS:
        payload = make_episode_record().to_dict()
        payload.pop(block)
        with pytest.raises(ContinuityContractError):
            EpisodeRecord.from_dict(payload)


def test_a_record_rejects_an_eleventh_block():
    payload = make_episode_record().to_dict()
    payload["goal_state"] = {"status": "done"}
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


def test_a_record_round_trips_through_strict_parsing():
    record = make_episode_record()
    assert EpisodeRecord.from_dict(parse_json_object(canonical_bytes(record.to_dict()).decode())) == record


def test_the_record_names_no_retired_field():
    """AGENTS.md section 3: there is no goal / goal_state / goal-reflection in the v2 cascade."""
    text = canonical_bytes(make_episode_record().to_dict()).decode()
    for retired in ("goal_state", "goal_reflection", '"goal"'):
        assert retired not in text


# ── the five independent outcome axes ────────────────────────────────────────


def test_each_axis_declares_its_own_vocabulary():
    assert CASCADE_STATUSES == ("completed", "failed", "deferred", "interrupted", "unknown")
    assert ACTION_STATUSES == (
        "succeeded",
        "failed",
        "partial",
        "blocked",
        "cancelled",
        "skipped",
        "not_attempted",
        "unknown",
    )
    assert TASK_OUTCOMES == (
        "success",
        "failure",
        "partial",
        "blocked",
        "cancelled",
        "not_applicable",
        "unknown",
    )
    assert VERIFICATION_STATUSES == (
        "passed",
        "failed",
        "partial",
        "not_performed",
        "not_applicable",
        "unknown",
    )
    assert FEEDBACK_STATUSES == ("not_received", "supports", "contradicts", "mixed", "unknown")


def test_a_completed_cascade_with_a_failed_task_and_a_failed_check_is_truthful():
    """Acceptance case C01 + C02: cascade completion is not task success."""
    outcome = EpisodeOutcome.from_dict(
        {
            "cascade_status": "completed",
            "action_status": "succeeded",
            "task_outcome": "failure",
            "verification_status": "failed",
            "feedback_status": "not_received",
            "assessments": {
                "task_outcome": {
                    "author": "host",
                    "evidence_refs": ["evt-0003"],
                    "criteria_revision": "acceptance@1",
                },
                "verification_status": {
                    "author": "host",
                    "verifier": "pytest",
                    "evidence_refs": ["evt-0004"],
                    "criteria_revision": "acceptance@1",
                    "artifact_revision": "artifact@2",
                },
            },
        }
    )
    assert outcome.cascade_status == "completed" and outcome.task_outcome == "failure"


def test_a_tool_success_with_no_check_stays_unverified():
    """C02: result remains unverified, and that is not the same as failed."""
    outcome = make_episode_record(
        outcome={
            "cascade_status": "completed",
            "action_status": "succeeded",
            "task_outcome": "unknown",
            "verification_status": "not_performed",
            "feedback_status": "not_received",
            "assessments": {},
        }
    ).outcome
    assert outcome.verification_status == "not_performed"
    assert outcome.verification_status != "failed"


def test_an_authorized_cancellation_is_not_a_reasoning_failure():
    """C05."""
    outcome = make_episode_record(
        outcome={
            "cascade_status": "completed",
            "action_status": "cancelled",
            "task_outcome": "cancelled",
            "verification_status": "not_applicable",
            "feedback_status": "not_received",
            "assessments": {},
        }
    ).outcome
    assert outcome.action_status == "cancelled" and outcome.cascade_status == "completed"


def test_a_legitimate_idle_outcome_invents_no_task():
    """C06."""
    outcome = make_episode_record(
        outcome={
            "cascade_status": "completed",
            "action_status": "not_attempted",
            "task_outcome": "not_applicable",
            "verification_status": "not_applicable",
            "feedback_status": "not_received",
            "assessments": {},
        }
    ).outcome
    assert outcome.task_outcome == "not_applicable"


@pytest.mark.parametrize(
    "axis,bad",
    [
        ("cascade_status", "succeeded"),
        ("action_status", "success"),
        ("task_outcome", "succeeded"),
        ("verification_status", "verified"),
        ("feedback_status", "positive"),
    ],
)
def test_an_unknown_status_name_on_any_axis_is_rejected(axis, bad):
    payload = make_episode_record().outcome.to_dict()
    payload[axis] = bad
    with pytest.raises(ContinuityContractError):
        EpisodeOutcome.from_dict(payload)


def test_an_axis_cannot_be_omitted():
    for axis in ("cascade_status", "action_status", "task_outcome", "verification_status", "feedback_status"):
        payload = make_episode_record().outcome.to_dict()
        payload.pop(axis)
        with pytest.raises(ContinuityContractError):
            EpisodeOutcome.from_dict(payload)


def test_a_model_self_report_is_not_a_verification_receipt():
    payload = make_episode_record().outcome.to_dict()
    payload["verification_status"] = "passed"
    payload["assessments"] = {"verification_status": {"author": "model", "evidence_refs": []}}
    with pytest.raises(ContinuityContractError):
        EpisodeOutcome.from_dict(payload)


def test_a_passed_verification_needs_a_check_reference():
    payload = make_episode_record().outcome.to_dict()
    payload["verification_status"] = "passed"
    payload["assessments"] = {"verification_status": {"author": "host", "verifier": "pytest", "evidence_refs": []}}
    with pytest.raises(ContinuityContractError):
        EpisodeOutcome.from_dict(payload)


def test_memory_commit_status_is_not_a_field_of_the_sealed_record():
    """CONTRACTS.md section 3: it is exposed by EpisodeCommitView (WO-07), never guessed here."""
    as_dict = make_episode_record().to_dict()
    assert "memory_commit_status" not in as_dict
    assert "memory_commit_status" not in as_dict["outcome"]
    assert MEMORY_COMMIT_STATUSES == (
        "pending",
        "canonical_committed",
        "recall_published",
        "failed",
        "unknown",
    )


# ── temporal, origin, objective ──────────────────────────────────────────────


def test_opening_and_closing_are_observations_and_arrival_is_recorded_separately():
    temporal = make_episode_record().temporal
    assert isinstance(temporal["opened_at"], Observation)
    assert isinstance(temporal["closed_at"], Observation)
    assert isinstance(temporal["queue_arrival_seq"], Observation)


def test_a_concurrent_commit_set_must_carry_its_completeness_qualifier():
    """The live carrier already knows this (`causal_parents_complete`); the record must too."""
    payload = make_episode_record().to_dict()
    payload["temporal"]["concurrent_commits"] = {"episode_ids": ["ep-other"]}
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


def test_a_short_concurrent_commit_set_says_so_rather_than_implying_completeness():
    record = make_episode_record(
        temporal_overrides={
            "concurrent_commits": {"episode_ids": ["ep-other"], "complete": False},
        }
    )
    assert record.temporal["concurrent_commits"]["complete"] is False


def test_origin_binds_the_triggering_input_and_the_authority_snapshot():
    origin = make_episode_record().origin
    assert origin["trigger"]["opened_by"]
    assert isinstance(origin["authority_snapshot_ref"], Observation)
    assert isinstance(origin["initial_trajectory_ref"], Observation)


def test_objective_records_criteria_with_the_revision_they_were_set_at():
    objective = make_episode_record().objective
    assert objective["acceptance_criteria"]
    assert objective["criteria_revision"]


def test_an_objective_is_not_a_persistent_goal_engine():
    payload = make_episode_record().to_dict()
    payload["objective"]["goal_id"] = "goal-1"
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


# ── events block ─────────────────────────────────────────────────────────────


def test_the_events_block_keeps_reference_order():
    record = make_episode_record(event_count=3)
    assert [ref.record_id for ref in record.events["refs"]] == ["evt-0001", "evt-0002", "evt-0003"]


def test_a_referenced_event_whose_bytes_were_not_retained_is_unavailable_not_summarized():
    record = make_episode_record(
        events_overrides={
            "by_value": (),
            "coverage": {
                "retained": 0,
                "referenced": 1,
                "unavailable_refs": ("evt-0001",),
                "reason": "the event payload was not retained by this build",
            },
        }
    )
    assert record.events["coverage"]["unavailable_refs"] == ("evt-0001",)


def test_the_events_block_requires_an_integrity_hash_per_retained_event():
    payload = make_episode_record(event_count=1).to_dict()
    payload["events"]["integrity"] = {}
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


def test_an_event_reference_must_be_a_typed_reference():
    payload = make_episode_record().to_dict()
    payload["events"]["refs"] = ["evt-0001"]
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


# ── metacognition keeps the shared control object unchanged ───────────────────


def test_metacognition_carries_the_validated_shared_control_object():
    from schema.omega_control_decision import OmegaControlDecision

    control = make_episode_record().metacognition["control"]
    assert isinstance(control, OmegaControlDecision)
    assert control.decision in ("DONE", "PRESSURE", "ASK_OPERATOR")


def test_metacognition_rejects_a_control_object_the_shared_validator_would_refuse():
    payload = make_episode_record().to_dict()
    payload["metacognition"]["control"]["decision"] = "RETRY"
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


def test_a_done_control_still_cannot_carry_a_gap():
    payload = make_episode_record().to_dict()
    payload["metacognition"]["control"]["decision"] = "DONE"
    payload["metacognition"]["control"]["assessment"]["gap"] = "something is missing"
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


# ── learning never predicts its own commit ───────────────────────────────────


def test_learning_records_identities_and_known_receipts_only():
    learning = make_episode_record().learning
    assert "proposals" in learning and "applied_receipts" in learning
    for forbidden in ("will_commit", "expected_receipt", "predicted_effect"):
        assert forbidden not in learning


def test_learning_rejects_a_predicted_commit():
    payload = make_episode_record().to_dict()
    payload["learning"]["will_commit"] = True
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


# ── coverage is required, and empty-but-observed is a real answer ─────────────


def test_coverage_names_what_was_captured_and_what_was_not():
    coverage = make_episode_record().coverage
    for key in ("captured", "unrecorded", "inaccessible", "redacted", "causal_gaps"):
        assert key in coverage


def test_an_episode_that_captured_everything_still_states_its_coverage():
    record = make_episode_record(
        coverage_overrides={"unrecorded": (), "inaccessible": (), "redacted": (), "causal_gaps": ()}
    )
    assert record.coverage["unrecorded"] == ()
    assert "captured" in record.coverage


def test_coverage_cannot_be_omitted_to_imply_completeness():
    payload = make_episode_record().to_dict()
    payload["coverage"] = {}
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(payload)


# ── isolation ────────────────────────────────────────────────────────────────


def test_mutating_an_input_block_after_construction_cannot_mutate_the_record():
    objective = {
        "original_request": "build the thing",
        "acceptance_criteria": ["ruff clean"],
        "criteria_revision": "acceptance@1",
        "preconditions": [],
    }
    record = make_episode_record(objective=objective)
    objective["original_request"] = "tampered"
    objective["acceptance_criteria"].append("injected")
    assert record.objective["original_request"] == "build the thing"
    assert tuple(record.objective["acceptance_criteria"]) == ("ruff clean",)


def test_two_records_built_from_the_same_dict_do_not_share_mutable_state():
    payload = make_episode_record().to_dict()
    first = EpisodeRecord.from_dict(payload)
    second = EpisodeRecord.from_dict(payload)
    assert first == second
    assert first.objective is not second.objective


# ── the real producer's nested outcome pack is the extraction boundary ────────


def test_the_nested_global_outcome_pack_the_real_producer_writes_is_the_source_shape():
    """WO-02 Step 5: build the pack with the REAL producer, not the old flat fixture.

    `write_outcome` stores `asdict(GlobalOutcomePack)`, so the status a window judges by
    lives at `outcome["outcome"]["status"]`. The flat shape a previous fixture wrote is the
    reason `_episode_spine` read one level too shallow for months (WO-01, live 2026-09-20).
    """
    from global_loop.package.run import GlobalOutcomePack, global_outcome_pack_to_dict

    pack = GlobalOutcomePack(
        episode_id="ep-0001",
        outcome={"status": "failed", "summary": "the build looped", "intent": "USE_TOOL"},
        observations={},
        artifacts={},
        followups=[],
        consistency={},
    )
    nested = global_outcome_pack_to_dict(pack)

    assert nested["outcome"]["status"] == "failed"
    assert "status" not in nested, "the pack is nested; a flat status would hide the real one"

    from schema.episode_record import cascade_and_task_from_outcome_pack

    axes = cascade_and_task_from_outcome_pack(nested, cascade_status="completed")
    assert axes["cascade_status"] == "completed"
    assert axes["task_outcome"] == "failure"
    assert axes["evidence"]["status_path"] == "outcome.outcome.status"


def test_extraction_refuses_a_flat_pack_rather_than_reading_an_absent_status():
    from schema.episode_record import cascade_and_task_from_outcome_pack

    with pytest.raises(ContinuityContractError):
        cascade_and_task_from_outcome_pack({"status": "failed"}, cascade_status="completed")


def test_extraction_reports_unknown_when_the_producer_recorded_no_status():
    from global_loop.package.run import GlobalOutcomePack, global_outcome_pack_to_dict
    from schema.episode_record import cascade_and_task_from_outcome_pack

    nested = global_outcome_pack_to_dict(
        GlobalOutcomePack(
            episode_id="ep-0002",
            outcome={"summary": ""},
            observations={},
            artifacts={},
            followups=[],
            consistency={},
        )
    )
    axes = cascade_and_task_from_outcome_pack(nested, cascade_status="completed")
    assert axes["task_outcome"] == "unknown"


def test_a_canonical_ref_to_the_record_is_content_addressed():
    record = make_episode_record()
    ref = CanonicalRef(
        record_id="9.01.ep0001",
        record_version=1,
        content_sha256=record.content_sha256(),
    )
    assert ref.content_sha256 == record.content_sha256()
    assert record.content_sha256() == make_episode_record().content_sha256()


def test_an_event_by_value_inside_the_record_is_the_same_contract_as_a_standalone_event():
    record = make_episode_record(event_count=1)
    assert record.events["by_value"][0] == make_event(event_id="evt-0001", event_seq=1)
