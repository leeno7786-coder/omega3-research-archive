"""WO-06 Steps 3–4 — the post-action fold survives content assembly without flattening.

Round trip, in the order's words: actual GlobalOutcomePack values and Omega control survive
content assembly without flattening or alias guessing. Global B must bind the full phase
evidence and the exact admitted cross-lane mail into the pack it derives; Omega's validated
control, its attributed assessment, and its proposals — lessons, lane directions, successor
resolution conditions — must come through intact, and stay PROPOSALS: nothing here claims they
were accepted or published.

The Omega result is produced by the real lane parser (``meta_omega.core.parse_lane``) and placed
on the carrier exactly as ``cognition_chain._apply_omega_episode_result`` places it.
"""

from __future__ import annotations

import pytest

from tests.continuity.test_outcome_axes import (
    CRITERION,
    SHA_A,
    THREAD,
    bucket,  # noqa: F401 - pytest fixture
    build,
    fold,
    outcome_of,
    revision,
    verified,
)


def _phase_evidence(carrier) -> dict:
    pack = carrier.global_["outcome"]
    evidence = pack.get("phase_evidence")
    assert isinstance(evidence, dict), (
        "the GlobalOutcomePack carries no phase evidence: Meta B's checks and AUTO's failure "
        "cause stop at Global B, flattened into one status string"
    )
    return evidence


# ── Step 3: Global B binds the full phase evidence ──────────────────────────


def test_the_real_pack_binds_meta_b_and_auto_evidence_verbatim(bucket):  # noqa: F811
    revision(1, SHA_A)
    verified(1, SHA_A, passed=False)
    carrier = fold(build("fail", failure_kind="tests_failed", failure_reason="2 tests failed"))
    reflection = carrier.meta["reflection"]

    evidence = _phase_evidence(carrier)
    meta_b = evidence["meta_b"]
    assert meta_b["state"] == "observed"
    assert meta_b["value"]["execution"] == reflection["execution"]
    assert meta_b["value"]["checks"] == reflection["checks"]
    assert meta_b["value"]["consistency"] == reflection["consistency"]

    auto = evidence["auto"]
    assert auto["state"] == "observed"
    assert auto["value"]["cycle_kind"] == "fail"
    assert auto["value"]["failure_kind"] == "tests_failed"
    assert auto["value"]["failure_reason"] == "2 tests failed"


def test_absent_meta_b_context_is_distinguishable_from_an_empty_one(bucket):  # noqa: F811
    """Global B's recovery path runs with no Meta B reflection. That is missing context, and it
    must never read like a reflection that legitimately found nothing."""
    from global_loop.loop import run_global_b

    carrier = build("success")
    run_global_b(carrier, {})

    meta_b = _phase_evidence(carrier)["meta_b"]
    assert meta_b["state"] == "not_captured"
    assert meta_b["value"] is None and meta_b["reason"]


def test_the_exact_admitted_cross_lane_mail_is_bound_into_the_pack(bucket):  # noqa: F811
    from global_loop import intersections as mail

    pinned = mail.pin_mail(thread_id=THREAD, delivery_point="omega_reason",
                           purpose="operator_instruction", requirement="required",
                           source_actor="operator", source_episode_id="ep-op",
                           payload={"text": "keep the parser dependency-free"})
    carrier = fold(build("success"))

    record = carrier.global_["omega_mail"]
    bound = carrier.global_["outcome"].get("cross_lane_mail")
    assert isinstance(bound, dict), "the pack does not say which cross-lane mail was admitted"
    assert bound["disposition"] == "bound"
    assert [ref["mail_id"] for ref in bound["mail_refs"]] == [pinned["mail_id"]]
    assert bound["mail_refs"] == [
        {"mail_id": row["mail_id"], "generation": row["generation"]} for row in record["receipts"]
    ], "the references must be the exact admitted identities, lease internals excluded"
    assert bound["cutoff_seq"] == record["cutoff_seq"]
    assert bound["omitted_required_mail_ids"] == []
    assert "lease_id" not in repr(bound["mail_refs"])


def test_no_admitted_mail_is_a_decided_empty_not_a_missing_one(bucket):  # noqa: F811
    carrier = fold(build("success"))

    bound = carrier.global_["outcome"].get("cross_lane_mail")
    assert isinstance(bound, dict)
    assert bound["disposition"] == "none" and bound["mail_refs"] == []


# ── Step 4: Omega's control, assessment and proposals ───────────────────────


def _parsed_omega_result(carrier):
    """A real OmegaEpisodeResult, through the real strict parser, from a real packet."""
    from omega_loop.episode.run import _packet_from_episode
    from organ.promptorgan.packs.meta_omega.core import parse_lane

    packet = _packet_from_episode(carrier)
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how"), "the parser build")
    control = {
        "assessment": {**assessment, "desired_state": "parser tests pass",
                       "observed_state": "two tests fail", "gap": "two failing tests",
                       "evidence": ["pytest: 2 failed"]},
        "decision": "PRESSURE",
        "pressure": {"intent": "USE_SKILL", "confidence": 0.8,
                     "reason": "fix the two failing parser tests",
                     "setpoints": {}, "desired_state": "parser tests pass",
                     "observed_state": "two tests fail",
                     "resolution_conditions": [f"{CRITERION} on parser.py"],
                     "domain": "parsing"},
        "synthesis": "The build is close; two tests still fail.",
    }
    payload = {
        "control": control,
        "episode_assessment": {"completed": False, "quality": 0.4, "reason": "tests fail"},
        "what_changed": {"learned": [], "did": ["wrote parser.py"], "verified": [],
                         "failed": ["2 tests"]},
        "decisions": [{"statement": "keep the tokenizer hand-written",
                       "rationale": "the grammar is tiny"}],
    }
    return parse_lane(payload, packet)


def _metacognition(carrier):
    from pressure_loop import episode_journal

    assemble = getattr(episode_journal, "metacognition_content", None)
    assert assemble is not None, (
        "there is no metacognition content assembler: Omega's decision and proposals reach the "
        "record only as an unvalidated blob"
    )
    return assemble(carrier)


def test_omega_control_and_proposals_survive_content_assembly(bucket):  # noqa: F811
    from schema.episode_record import _checked_metacognition
    from schema.omega_control_decision import OmegaControlDecision

    carrier = fold(build("fail"))
    result = _parsed_omega_result(carrier)
    global_ = dict(carrier.global_)
    global_["omega_result"] = result.to_dict()  # exactly as _apply_omega_episode_result stores it
    carrier.global_ = global_

    content = _metacognition(carrier)
    assert content.state == "observed"
    value = content.to_dict()["value"]  # the JSON form the record persists

    assert OmegaControlDecision.from_dict(value["control"]).to_dict() == result.control_decision, (
        "the shared control decision must survive verbatim after validation"
    )
    assert value["model_assessment"]["author"] == "model"
    assert value["model_assessment"]["episode_assessment"] == result.episode_assessment
    assert value["model_assessment"]["what_changed"] == result.what_changed

    by_kind = {row["kind"]: row for row in value["proposals"]}
    successor = by_kind["successor"]
    assert successor["status"] == "proposed"
    assert successor["intent"] == "USE_SKILL"
    assert successor["resolution_conditions"] == [f"{CRITERION} on parser.py"]
    assert successor["source_refs"]["episode_id"] == carrier.episode_id
    decision = by_kind["decision"]
    assert decision["status"] == "proposed"
    assert decision["statement"] == "keep the tokenizer hand-written"
    assert all(row["status"] == "proposed" for row in value["proposals"]), (
        "nothing here has been accepted or published; a proposal must say so"
    )

    # The assembled block is accepted by the frozen WO-02 contract as it stands.
    _checked_metacognition(value)


def test_a_failed_omega_fold_leaves_metacognition_unavailable_with_its_cause(bucket):  # noqa: F811
    carrier = fold(build("fail"))
    carrier.meta["omega_fold_failure"] = "control contract rejected: decision missing"

    content = _metacognition(carrier)
    assert content.state == "unavailable"
    assert content.value is None
    assert "control contract rejected" in content.reason


# ── Round trip of the outcome block ─────────────────────────────────────────


def test_the_outcome_block_round_trips_through_the_frozen_contract(bucket):  # noqa: F811
    from schema.episode_record import EpisodeOutcome

    revision(1, SHA_A)
    verified(1, SHA_A, passed=False)
    carrier = fold(build("fail"))

    outcome = outcome_of(carrier)
    again = EpisodeOutcome.from_dict(outcome.to_dict())
    assert again == outcome
    assert again.assessments["verification_status"]["check_refs"]


def test_the_journal_row_carries_the_same_axes_through_its_own_round_trip(bucket):  # noqa: F811
    from pressure_loop.episode_journal import PriorEpisode, prior_episode_from_episode

    revision(1, SHA_A)
    verified(1, SHA_A, passed=False)
    carrier = fold(build("fail"))

    row = prior_episode_from_episode(carrier)
    axes = getattr(row, "outcome_axes", None)
    assert axes == outcome_of(carrier).to_dict(), (
        "the durable journal row must carry the same five axes, or every rate read from it "
        "falls back to guessing"
    )
    assert PriorEpisode.from_dict(row.to_dict()).outcome_axes == axes


@pytest.mark.parametrize("field", ["phase_evidence", "cross_lane_mail"])
def test_the_new_pack_fields_avoid_the_retired_goal_vocabulary(bucket, field):  # noqa: F811
    """The v2 outcome-shape guard forbids 'goal' / 'goal_state' / 'target' keys anywhere under
    episode.global_, and the pack rides there."""
    carrier = fold(build("success"))
    text = repr(carrier.global_["outcome"].get(field))
    for banned in ("'goal'", "'goal_state'", "'target'"):
        assert banned not in text
