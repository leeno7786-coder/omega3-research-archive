"""WO-02 — snapshot, ten-member manifest, evidence packet, and the window response.

Two rules carry most of the weight here:

* **coverage is the host's arithmetic, not the model's claim.** `episode_assessments` must
  hold exactly one row per focal member; a duplicate or a miss fails. The live validator
  (`omega_loop/window/run.py::validate_window_payload`) has no such rule because the live
  reflection has no per-episode assessments at all.
* **the optional shared cognitive delta survives.** `decisions`, `user_constraints` and
  `lane_injections` are live cross-lane behaviour (`meta_omega/core.py::COGNITIVE_DELTA`).
  CONTRACTS.md section 6 refines the older "exactly three top-level fields" draft precisely
  so this window work cannot delete them.
"""

from __future__ import annotations

import pytest

from schema.continuity_json import (
    ContinuityContractError,
    Observation,
    WindowCapacityExceeded,
    WindowMembershipError,
    WindowOutputError,
    WindowReferenceError,
    canonical_bytes,
    parse_json_object,
)
from schema.window_evidence import (
    WINDOW_PACKET_BLOCKS,
    HistoryBundle,
    HistoryLimits,
    SnapshotRef,
    WindowEvidencePacket,
    WindowManifest,
    WindowMetrics,
)
from schema.window_reflection import (
    COGNITIVE_DELTA_KEYS,
    REFLECTION_FIELDS,
    REFLECTION_LIMITS,
    TRAJECTORY_OPERATIONS,
    WINDOW_RESPONSE_KEYS,
    ValidatedWindowResponse,
    validate_window_response_payload,
)
from tests.helpers.continuity_records import (
    TEN_MEMBERS,
    make_packet,
    make_snapshot_ref,
    make_valid_response,
    make_window_manifest,
)


# ── SnapshotRef binds retained versions, never "today" ────────────────────────


def test_a_snapshot_binds_the_generation_the_coverage_clock_and_the_cutoff():
    snapshot = make_snapshot_ref()
    for key in (
        "generation_id",
        "coverage_clock",
        "visibility_cutoff",
        "trajectory_revision_at_start",
        "trajectory_revision_at_cutoff",
        "authority_snapshot_ref",
        "reader_policy_revision",
    ):
        assert key in snapshot.to_dict(), key


def test_a_snapshot_generation_id_is_a_full_hash():
    with pytest.raises(ContinuityContractError):
        make_snapshot_ref(generation_id="gen-1")


def test_a_coverage_clock_compares_identity_before_number():
    """core.timekeeper.CoverageClock semantics: a different epoch is a regression."""
    snapshot = make_snapshot_ref()
    other_epoch = make_snapshot_ref(coverage_clock={"clock_epoch_id": "epoch-b", "commit_seq": 999})
    assert not snapshot.covers(other_epoch.coverage_clock)


def test_a_coverage_clock_rejects_a_boolean_sequence():
    with pytest.raises(ContinuityContractError):
        make_snapshot_ref(coverage_clock={"clock_epoch_id": "epoch-a", "commit_seq": True})


def test_an_unsequenced_source_inventory_digest_is_optional_but_typed():
    assert "unsequenced_inventory_digest" not in make_snapshot_ref().to_dict()
    with pytest.raises(ContinuityContractError):
        make_snapshot_ref(unsequenced_inventory_digest="not-a-hash")


# ── WindowManifest: exactly ten fresh non-window members ──────────────────────


def test_a_manifest_seals_ten_ordered_unique_members():
    manifest = make_window_manifest()
    assert manifest.member_count == 10
    assert manifest.member_ids == TEN_MEMBERS
    assert tuple(ref.record_id for ref in manifest.member_refs) == tuple(f"9.01.{m}" for m in TEN_MEMBERS)


def test_a_manifest_binds_its_configuration_fingerprint_and_snapshot():
    manifest = make_window_manifest()
    assert manifest.configuration_fingerprint
    assert isinstance(manifest.snapshot, SnapshotRef)


def test_a_duplicate_member_is_refused():
    with pytest.raises(WindowMembershipError):
        make_window_manifest(member_ids=TEN_MEMBERS[:9] + (TEN_MEMBERS[0],))


def test_a_sealed_cohort_of_fewer_than_ten_is_refused_by_default():
    with pytest.raises(WindowMembershipError):
        make_window_manifest(member_ids=TEN_MEMBERS[:9])


def test_an_eligible_failure_remains_a_member():
    """The live window already keeps these in cascade_status_by_episode; so must the manifest."""
    manifest = make_window_manifest(
        member_outcomes={TEN_MEMBERS[3]: "failed", TEN_MEMBERS[7]: "interrupted"}
    )
    assert manifest.member_count == 10
    assert manifest.member_outcome(TEN_MEMBERS[3]) == "failed"


def test_a_window_episode_cannot_be_its_own_member():
    with pytest.raises(WindowMembershipError):
        make_window_manifest(member_ids=TEN_MEMBERS[:9] + ("window-ep-0001",), window_id="window-ep-0001")


def test_a_partially_published_member_is_never_represented_as_complete():
    with pytest.raises(WindowMembershipError):
        make_window_manifest(member_commit_status={TEN_MEMBERS[0]: "pending"})


def test_a_manifest_records_completeness_explicitly():
    assert make_window_manifest().complete is True


def test_a_manifest_round_trips_through_strict_parsing():
    manifest = make_window_manifest()
    assert WindowManifest.from_dict(parse_json_object(canonical_bytes(manifest.to_dict()).decode())) == manifest


# ── HistoryBundle: not-searched is not empty ───────────────────────────────────


def test_history_distinguishes_not_run_from_empty_from_failed():
    for status, expected in (
        ("search_not_run", "search_not_run"),
        ("searched_no_matches", "searched_no_matches"),
        ("reader_failed", "reader_failed"),
    ):
        bundle = HistoryBundle.from_dict(
            {
                "primary_support": [],
                "prior_findings": [],
                "intervening_direction_changes": [],
                "links": [],
                "retrieval_audit": {
                    "query_policy_revision": "history@1",
                    "scope": "lobe-9 episodes at the cutoff",
                    "considered_ids": [],
                    "returned_ids": [],
                    "admitted_ids": [],
                    "omitted": [],
                    "counterexample_search_status": status,
                    "cap": 32,
                },
            }
        )
        assert bundle.retrieval_audit["counterexample_search_status"] == expected


def test_an_unknown_counterexample_search_status_is_rejected():
    with pytest.raises(ContinuityContractError):
        HistoryBundle.from_dict(
            {
                "primary_support": [],
                "prior_findings": [],
                "intervening_direction_changes": [],
                "links": [],
                "retrieval_audit": {
                    "query_policy_revision": "history@1",
                    "scope": "s",
                    "considered_ids": [],
                    "returned_ids": [],
                    "admitted_ids": [],
                    "omitted": [],
                    "counterexample_search_status": "none_found",
                    "cap": 32,
                },
            }
        )


def test_history_limits_must_be_positive_and_carry_their_policy_revision():
    limits = HistoryLimits(record_cap=32, token_candidate_cap=8000, retrieval_policy_revision="history@1")
    assert limits.record_cap == 32
    with pytest.raises(ContinuityContractError):
        HistoryLimits(record_cap=0, token_candidate_cap=8000, retrieval_policy_revision="history@1")
    with pytest.raises(ContinuityContractError):
        HistoryLimits(record_cap=32, token_candidate_cap=8000, retrieval_policy_revision="")


def test_a_prior_reflection_is_labelled_historical_and_does_not_count_as_fresh():
    packet = make_packet()
    assert packet.manifest.member_count == 10
    assert all(row["support_class"] == "historical" for row in packet.historical_support)


# ── WindowMetrics: explicit denominators ──────────────────────────────────────


def test_a_metric_carries_numerator_denominator_population_and_version():
    metrics = make_packet().metrics
    row = metrics.rows[0]
    for key in (
        "metric_id",
        "numerator",
        "denominator",
        "population_refs",
        "excluded",
        "unknown",
        "stratification",
        "computation_version",
    ):
        assert key in row, key


def test_a_metric_with_a_zero_denominator_is_refused():
    with pytest.raises(ContinuityContractError):
        WindowMetrics.from_dict(
            {
                "computation_version": "metrics@1",
                "rows": [
                    {
                        "metric_id": "verified_rate",
                        "numerator": 0,
                        "denominator": 0,
                        "population_refs": [],
                        "excluded": 0,
                        "unknown": 0,
                        "stratification": {},
                        "computation_version": "metrics@1",
                    }
                ],
            }
        )


def test_attempts_and_independent_cases_are_separate_metrics():
    """C04: three unchanged attempts in one job are attempts=3, independent cases=1."""
    metrics = make_packet(
        metric_rows=[
            {
                "metric_id": "attempts",
                "numerator": 3,
                "denominator": 3,
                "population_refs": ["job-0001"],
                "excluded": 0,
                "unknown": 0,
                "stratification": {},
                "computation_version": "metrics@1",
            },
            {
                "metric_id": "independent_cases",
                "numerator": 1,
                "denominator": 1,
                "population_refs": ["job-0001"],
                "excluded": 0,
                "unknown": 0,
                "stratification": {},
                "computation_version": "metrics@1",
            },
        ]
    ).metrics
    by_id = {row["metric_id"]: row for row in metrics.rows}
    assert by_id["attempts"]["numerator"] == 3
    assert by_id["independent_cases"]["numerator"] == 1


def test_unknown_independence_stays_unknown_rather_than_defaulting_to_one():
    metrics = make_packet(
        metric_rows=[
            {
                "metric_id": "independent_cases",
                "numerator": 0,
                "denominator": 2,
                "population_refs": ["job-0002"],
                "excluded": 0,
                "unknown": 2,
                "stratification": {},
                "computation_version": "metrics@1",
            }
        ]
    ).metrics
    assert metrics.rows[0]["unknown"] == 2


# ── WindowEvidencePacket: exactly eleven blocks, citations resolve ─────────────


def test_the_packet_declares_exactly_eleven_blocks():
    assert WINDOW_PACKET_BLOCKS == (
        "schema_version",
        "manifest",
        "baseline",
        "episodes",
        "temporal_links",
        "historical_support",
        "learning_followthrough",
        "open_threads",
        "metrics",
        "limitations",
        "evidence_table",
    )


def test_a_packet_carries_every_block_and_no_other():
    assert tuple(sorted(make_packet().to_dict())) == tuple(sorted(WINDOW_PACKET_BLOCKS))


def test_a_packet_rejects_a_twelfth_block():
    payload = make_packet().to_dict()
    payload["raw_bodies"] = []
    with pytest.raises(ContinuityContractError):
        WindowEvidencePacket.from_dict(payload)


def test_every_citation_key_resolves_to_shown_evidence():
    packet = make_packet()
    for key in packet.cited_keys():
        assert key in packet.evidence_table


def test_a_citation_to_an_omitted_payload_is_refused():
    """R37: a locator for an omitted blob is not evidence the model read it."""
    payload = make_packet().to_dict()
    payload["temporal_links"] = [
        {
            "claim": "the second attempt repeated the first",
            "relation_kind": "repeats",
            "episode_ids": list(TEN_MEMBERS[:2]),
            "support_refs": ["ev-omitted"],
            "counterevidence_refs": [],
            "uncertainty": "medium",
        }
    ]
    with pytest.raises(WindowReferenceError):
        WindowEvidencePacket.from_dict(payload)


def test_host_metadata_and_attributed_content_stay_separate():
    row = make_packet().evidence_table["ev-0001"]
    assert row["evidence_class"] in (
        "host_observation",
        "operator_statement",
        "external_content",
        "model_conclusion",
        "verification_result",
        "commit_receipt",
    )
    assert "attributed_to" in row


def test_a_packet_round_trips_through_strict_parsing():
    packet = make_packet()
    assert WindowEvidencePacket.from_dict(parse_json_object(canonical_bytes(packet.to_dict()).decode())) == packet


def test_the_packet_is_a_derived_view_and_says_which_snapshot_it_was_derived_from():
    packet = make_packet()
    assert packet.manifest.snapshot.generation_id == make_snapshot_ref().generation_id


# ── the window response ───────────────────────────────────────────────────────


def test_the_response_admits_three_required_keys_and_the_three_optional_deltas():
    assert WINDOW_RESPONSE_KEYS == ("control", "reflection", "trajectory_update")
    assert COGNITIVE_DELTA_KEYS == ("decisions", "user_constraints", "lane_injections")


def test_the_optional_cognitive_delta_survives_validation():
    """The live cross-lane feature. Deleting it would remove working behaviour."""
    payload = make_valid_response(
        decisions=[{"statement": "seal windows by finish line", "rationale": "commit order is memory order"}],
        user_constraints=[{"statement": "never ask twice for granted scope"}],
        lane_injections=[
            {
                "target_lane": "agent",
                "objective": "re-run the failing acceptance check",
                "constraints": ["do not widen scope"],
                "evidence_refs": ["ev-0001"],
                "requirement": "required",
            }
        ],
    )
    response = validate_window_response_payload(payload, member_ids=TEN_MEMBERS)
    assert response.decisions and response.user_constraints and response.lane_injections


def test_a_response_without_any_delta_is_equally_valid():
    response = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    assert response.decisions == () and response.lane_injections == ()


def test_a_fourth_top_level_key_is_refused():
    payload = make_valid_response()
    payload["mastery"] = [{"domain": "x"}]
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_the_model_cannot_return_host_receipts_in_place_of_host_state():
    payload = make_valid_response()
    payload["reflection"]["coverage"]["generation_id"] = "f" * 64
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_reflection_declares_exactly_ten_fields():
    assert REFLECTION_FIELDS == (
        "summary",
        "coverage",
        "episode_assessments",
        "temporal_findings",
        "patterns",
        "learning_followthrough",
        "thread_updates",
        "proactive_candidates",
        "limitations",
        "trajectory_assessment",
    )


def test_exactly_one_assessment_per_focal_member_is_required():
    response = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    assert tuple(row["episode_id"] for row in response.reflection["episode_assessments"]) == TEN_MEMBERS


def test_a_duplicate_episode_assessment_fails():
    payload = make_valid_response()
    payload["reflection"]["episode_assessments"][1]["episode_id"] = TEN_MEMBERS[0]
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_a_missing_episode_assessment_fails():
    payload = make_valid_response()
    payload["reflection"]["episode_assessments"].pop()
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_an_assessment_for_a_non_member_fails():
    payload = make_valid_response()
    payload["reflection"]["episode_assessments"][0]["episode_id"] = "ep-not-a-member"
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_the_host_recomputes_coverage_rather_than_trusting_the_declared_list():
    payload = make_valid_response()
    payload["reflection"]["coverage"]["member_ids"] = list(TEN_MEMBERS[:5])
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_an_empty_limitations_array_is_valid_when_nothing_is_added():
    payload = make_valid_response()
    payload["reflection"]["limitations"] = []
    assert validate_window_response_payload(payload, member_ids=TEN_MEMBERS).reflection["limitations"] == ()


@pytest.mark.parametrize(
    "field,limit",
    [
        ("temporal_findings", 8),
        ("patterns", 8),
        ("learning_followthrough", 16),
        ("thread_updates", 16),
        ("proactive_candidates", 4),
        ("limitations", 16),
    ],
)
def test_the_declared_output_maxima_are_enforced(field, limit):
    assert REFLECTION_LIMITS[field] == limit


def test_a_summary_over_four_thousand_characters_is_refused():
    payload = make_valid_response()
    payload["reflection"]["summary"] = "x" * 4001
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_a_claim_over_two_thousand_characters_is_refused():
    payload = make_valid_response()
    payload["reflection"]["patterns"][0]["claim"] = "y" * 2001
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_a_pattern_must_carry_a_falsifier():
    payload = make_valid_response()
    payload["reflection"]["patterns"][0].pop("falsifier")
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_a_proactive_candidate_is_never_a_delivery():
    response = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    candidate = response.reflection["proactive_candidates"][0]
    assert "delivery_conditions" in candidate
    assert "delivered" not in candidate and "delivered_at" not in candidate


def test_a_proactive_candidate_claiming_delivery_is_refused():
    payload = make_valid_response()
    payload["reflection"]["proactive_candidates"][0]["delivered"] = True
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_a_new_thread_is_a_request_not_a_model_minted_id():
    payload = make_valid_response()
    payload["reflection"]["thread_updates"][0] = {
        "new_thread_request": {"topic": "window hydration is not as-of", "evidence_refs": ["ev-0001"]},
        "proposed_state": "open",
        "change": "opened by this window",
        "evidence_refs": ["ev-0001"],
    }
    response = validate_window_response_payload(payload, member_ids=TEN_MEMBERS)
    assert "thread_ref" not in response.reflection["thread_updates"][0]


def test_a_model_minted_thread_id_is_refused():
    payload = make_valid_response()
    payload["reflection"]["thread_updates"][0]["thread_ref"] = {"record_id": "minted-by-model"}
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


# ── trajectory: one vocabulary, the schema's ──────────────────────────────────


def test_the_trajectory_operations_come_from_the_authoritative_producer():
    """WO-01 loss L4: three vocabularies disagreed. schema/omega_trajectory.py is authority."""
    from schema.omega_trajectory import TRAJECTORY_DECISION_VALUES

    assert TRAJECTORY_OPERATIONS == TRAJECTORY_DECISION_VALUES
    assert TRAJECTORY_OPERATIONS == ("establish", "idle", "alter", "abandon", "complete")


def test_the_retired_prompt_vocabulary_is_not_accepted():
    for stale in ("continue", "adjust"):
        payload = make_valid_response(trajectory_update={"operation": stale, "target_domain": "d", "reason": "r"})
        with pytest.raises(WindowOutputError):
            validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_a_null_trajectory_update_means_no_update():
    response = validate_window_response_payload(
        make_valid_response(trajectory_update=None), member_ids=TEN_MEMBERS
    )
    assert response.trajectory_update is None


def test_a_capability_without_a_domain_cannot_be_recorded():
    payload = make_valid_response(
        trajectory_update={"operation": "alter", "target_capability": "web_fetch", "reason": "r"}
    )
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_the_host_supplies_expected_state_revisions_not_the_model():
    payload = make_valid_response(
        trajectory_update={
            "operation": "alter",
            "target_domain": "memory",
            "reason": "r",
            "expected_trajectory_revision": "rev-9",
        }
    )
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_trajectory_assessment_is_not_a_second_update_channel():
    payload = make_valid_response()
    payload["reflection"]["trajectory_assessment"]["operation"] = "alter"
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_a_validated_response_binds_its_input_receipt():
    packet = make_packet()
    response = validate_window_response_payload(
        make_valid_response(), member_ids=TEN_MEMBERS, packet_digest=packet.content_sha256()
    )
    assert response.packet_digest == packet.content_sha256()


def test_a_response_round_trips_through_strict_parsing():
    response = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    again = ValidatedWindowResponse.from_dict(parse_json_object(canonical_bytes(response.to_dict()).decode()))
    assert again == response


# ── capacity is an explicit failure, never a silent truncation ────────────────


def test_structural_capacity_raises_rather_than_shrinking_the_cohort():
    packet = make_packet()
    with pytest.raises(WindowCapacityExceeded):
        packet.require_structural_capacity(available_items=1)


def test_the_item_count_makes_no_claim_about_tokens():
    """WO-02A obligation 1: the universal one-token-per-item claim was withdrawn."""
    packet = make_packet()
    assert packet.mandatory_item_count() > 0
    assert not hasattr(packet, "mandatory_item_floor")
    assert not hasattr(packet, "require_mandatory_fits")


def test_a_capacity_failure_preserves_the_sealed_membership():
    packet = make_packet()
    try:
        packet.require_structural_capacity(available_items=1)
    except WindowCapacityExceeded as exc:
        assert exc.member_count == 10
        assert tuple(exc.member_ids) == TEN_MEMBERS


def test_an_unavailable_observation_in_the_packet_cannot_carry_a_value():
    payload = make_packet().to_dict()
    payload["baseline"]["prior_window_ref"] = {
        "state": "unavailable",
        "value": {"record_id": "smuggled"},
        "reason": "no prior window",
    }
    with pytest.raises(ContinuityContractError):
        WindowEvidencePacket.from_dict(payload)


def test_the_first_window_has_no_prior_baseline_and_says_so():
    packet = make_packet(prior_window_ref=Observation.missing("not_applicable", reason="first window"))
    assert packet.baseline["prior_window_ref"].state == "not_applicable"
