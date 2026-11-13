"""WO-02A — the positive and negative controls for each repaired invariant family.

`test_wo02_review_regressions.py` holds the eight reproductions the independent review
supplied. This file is the rest of what the correction order asks for: the neighbouring
negatives that prove the repair is a rule rather than a patch for one input, and the positives
that prove the rule did not swallow legitimate data.

One control per requirement, grouped by the order's C-numbers.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from schema.continuity_json import (
    ContinuityContractError,
    EventIdentityConflict,
    Observation,
    WindowMembershipError,
    WindowOutputError,
    WindowReferenceError,
    canonical_bytes,
    contract_types,
    parse_json_object,
)
from schema.episode_event import SEALED_EVENT_FIELDS, EpisodeEvent
from schema.episode_record import EpisodeOutcome
from schema.window_evidence import WindowEvidencePacket, WindowManifest
from schema.window_reflection import ValidatedWindowResponse, validate_window_response_payload
from tests.helpers.continuity_records import (
    TEN_MEMBERS,
    make_event,
    make_packet,
    make_valid_response,
    make_window_manifest,
)


# ── C1 — numeric admission at the raw boundary ────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ['{"x":1e9999}', '{"x":-1e9999}', '{"x":1E400}', '{"nested":{"deep":[1e999]}}', '{"x":-1E999}'],
)
def test_every_overflowing_literal_is_refused_at_the_raw_boundary(raw):
    with pytest.raises(ContinuityContractError):
        parse_json_object(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a":1.5}', {"a": 1.5}),
        ('{"a":-2}', {"a": -2}),
        ('{"a":0.0}', {"a": 0.0}),
        ('{"a":1e308}', {"a": 1e308}),
        ('{"a":-1e-308}', {"a": -1e-308}),
        ('{"a":[1,2.5,-3]}', {"a": [1, 2.5, -3]}),
    ],
)
def test_ordinary_finite_numbers_are_unchanged(raw, expected):
    """The repair must not cost legitimate numerics."""
    assert parse_json_object(raw) == expected


def test_duplicate_key_and_trailing_material_rejection_still_hold():
    """C1 must not have displaced the rules that were already there."""
    with pytest.raises(ContinuityContractError):
        parse_json_object('{"a":1,"a":2}')
    with pytest.raises(ContinuityContractError):
        parse_json_object('{"a":1} trailing')


# ── C2 — validate before freezing or converting ───────────────────────────────


@pytest.mark.parametrize("key", [1, 1.5, True, None, (1, 2)])
def test_a_non_string_object_key_is_refused_before_conversion(key):
    with pytest.raises(ContinuityContractError):
        Observation.observed({key: "value"})


def test_a_key_collision_is_never_resolved_by_overwriting():
    with pytest.raises(ContinuityContractError) as excinfo:
        Observation.observed({1: "first", "1": "second"})
    assert "coerced" in str(excinfo.value)


def test_legitimate_observed_emptiness_still_survives():
    """The strictness must not eat an explicitly empty string, array or object."""
    assert Observation.observed("").value == ""
    assert Observation.observed([]).value == ()
    assert Observation.observed({}).value == {}
    assert Observation.observed(0).value == 0
    assert Observation.observed(False).value is False


def test_a_non_json_value_is_refused_at_construction_not_at_hashing():
    with pytest.raises(ContinuityContractError):
        make_event(payload={"when": object()})
    with pytest.raises(ContinuityContractError):
        make_event(payload={"when": {1, 2}})


def test_only_registered_contract_types_serialize_through_their_own_to_dict():
    class Pretender:
        def to_dict(self):
            return {"smuggled": True}

    assert Pretender not in contract_types()
    with pytest.raises(ContinuityContractError):
        canonical_bytes({"x": Pretender()})


@pytest.mark.parametrize("bad", ["abc", None, 7, True, {}, {"a": 1}])
def test_a_reference_array_is_never_inferred_from_a_non_array(bad):
    body = make_event().to_dict()
    body["evidence_refs"] = bad
    with pytest.raises(ContinuityContractError):
        EpisodeEvent.from_dict(body)


@pytest.mark.parametrize("bad", ["abc", None, 7, True, {"a": 1}])
def test_direct_construction_refuses_the_same_non_arrays_as_deserialization(bad):
    """Consistency: both paths must enforce the declared type identically."""
    with pytest.raises(ContinuityContractError):
        make_event(evidence_refs=bad)


@pytest.mark.parametrize("bad", ["abc", None, 7, {"a": 1}])
def test_manifest_members_and_metric_rows_are_type_checked_before_iteration(bad):
    with pytest.raises(ContinuityContractError):
        make_window_manifest(members=bad)


def test_defensive_copy_semantics_survive_the_repair():
    payload = {"verdict": "passed", "checks": [{"name": "ruff"}]}
    event = make_event(payload=payload)
    payload["verdict"] = "tampered"
    payload["checks"][0]["name"] = "tampered"
    assert event.payload["verdict"] == "passed"
    assert event.payload["checks"][0]["name"] == "ruff"
    returned = event.to_dict()
    returned["payload"]["verdict"] = "tampered"
    assert event.payload["verdict"] == "passed"


# ── C3 — replay identity covers attribution and ordering ──────────────────────


def test_the_sealed_field_set_is_declared_in_exactly_one_place():
    assert "producer" in SEALED_EVENT_FIELDS
    assert "event_seq" in SEALED_EVENT_FIELDS
    for name in SEALED_EVENT_FIELDS:
        assert hasattr(make_event(), name), name


@pytest.mark.parametrize(
    "field,value",
    [
        ("episode_id", "ep-other"),
        ("thread_id", "thread-other"),
        ("operation_id", "op-other"),
        ("event_seq", 9),
        ("phase_attempt", 2),
        ("phase", "deepsleep"),
        ("event_kind", "something_else"),
        ("observed_at", "2026-03-04T05:06:08.000000+00:00"),
        ("boot_id", "boot-00000000000000000000000000000002"),
        ("status", "failed"),
        ("evidence_class", "model_conclusion"),
        ("turn_id", "turn-other"),
        ("arrival_seq", 99),
        ("pipeline_id", 3),
    ],
)
def test_changing_any_sealed_field_is_a_replay_conflict(field, value):
    """C3: one identity, one sealed occurrence. Every family gets a case."""
    event = make_event(event_id="evt-sealed")
    changed = replace(event, **{field: value})
    with pytest.raises(EventIdentityConflict) as excinfo:
        event.require_same_occurrence(changed)
    assert field in str(excinfo.value)


def test_changing_the_producer_attribution_is_a_replay_conflict():
    event = make_event(event_id="evt-sealed")
    changed = replace(
        event,
        producer={
            "component": "tests.helpers.continuity_records",
            "component_version": "wo02",
            "authority": "host",
            "model": "a-different-model",
        },
    )
    with pytest.raises(EventIdentityConflict) as excinfo:
        event.require_same_occurrence(changed)
    assert "producer" in str(excinfo.value)


def test_changing_the_occurred_at_observation_is_a_replay_conflict():
    event = make_event(event_id="evt-sealed")
    changed = replace(event, occurred_at=Observation.missing("not_captured", reason="lost"))
    with pytest.raises(EventIdentityConflict):
        event.require_same_occurrence(changed)


def test_an_unchanged_replay_is_still_idempotent():
    first = make_event(event_id="evt-sealed")
    assert first.require_same_occurrence(make_event(event_id="evt-sealed")) is first


def test_two_operations_may_share_semantic_content_and_remain_different_events():
    """C3's converse: content similarity is not identity, and must stay available."""
    first = make_event(event_id="evt-a", operation_id="op-a", event_seq=1)
    second = make_event(event_id="evt-b", operation_id="op-b", event_seq=2)
    assert first.content_sha256() == second.content_sha256()
    assert first.occurrence_id() != second.occurrence_id()
    assert first.sealed_sha256() != second.sealed_sha256()


# ── C4 — bijection with the sealed manifest ───────────────────────────────────


def test_a_substitution_that_preserves_the_row_count_is_still_refused():
    """A duplicate plus a miss keeps len() correct; the bijection catches it anyway."""
    body = make_packet().to_dict()
    body["episodes"][1] = copy.deepcopy(body["episodes"][0])
    with pytest.raises(WindowMembershipError):
        WindowEvidencePacket.from_dict(body)


def test_a_missing_member_is_named():
    body = make_packet().to_dict()
    body["episodes"] = body["episodes"][:-1]
    with pytest.raises(WindowMembershipError) as excinfo:
        WindowEvidencePacket.from_dict(body)
    assert TEN_MEMBERS[-1] in str(excinfo.value)


def test_reordered_capsules_violate_the_declared_order_policy():
    """The declared policy is the manifest's finish-line order, not set membership."""
    body = make_packet().to_dict()
    body["episodes"] = [body["episodes"][1], body["episodes"][0], *body["episodes"][2:]]
    with pytest.raises(WindowMembershipError) as excinfo:
        WindowEvidencePacket.from_dict(body)
    assert "order" in str(excinfo.value)


@pytest.mark.parametrize(
    "field,value",
    [("record_id", "9.01.not-this-episode"), ("record_version", 2), ("content_sha256", "a" * 64)],
)
def test_a_capsule_reference_must_agree_with_its_manifest_reference(field, value):
    body = make_packet().to_dict()
    body["episodes"][0]["ref"][field] = value
    with pytest.raises(WindowReferenceError):
        WindowEvidencePacket.from_dict(body)


def test_a_correct_packet_still_constructs():
    """The bijection must not refuse the legitimate case."""
    packet = make_packet()
    assert packet.manifest.member_ids == TEN_MEMBERS
    assert tuple(row["episode_id"] for row in packet.episodes) == TEN_MEMBERS


def test_a_stored_manifest_cannot_lower_its_own_membership_bar():
    body = make_window_manifest().to_dict()
    body["members"] = body["members"][:1]
    body["expected_member_count"] = 1
    with pytest.raises(WindowMembershipError) as excinfo:
        WindowManifest.from_dict(body)
    # Must fail for the LOWERED BAR, not merely for having one member: both raise the same
    # type, and a test that passes for the other reason would not be testing this rule.
    assert "expected_member_count" in str(excinfo.value)


# ── C5 — a verification verdict needs its own attribution ─────────────────────


@pytest.mark.parametrize("verdict", ["passed", "partial", "failed"])
def test_a_performed_verification_without_any_assessment_is_refused(verdict):
    with pytest.raises(ContinuityContractError) as excinfo:
        EpisodeOutcome("completed", "succeeded", "success", verdict, "not_received")
    assert "attribution" in str(excinfo.value) or "assessment" in str(excinfo.value)


@pytest.mark.parametrize("verdict", ["not_performed", "not_applicable", "unknown"])
def test_an_unperformed_or_unknown_verification_needs_no_attribution(verdict):
    """The axis exists to keep these distinct from a claimed check."""
    outcome = EpisodeOutcome("completed", "succeeded", "unknown", verdict, "not_received")
    assert outcome.verification_status == verdict


def test_a_performed_verification_needs_at_least_one_reference():
    with pytest.raises(ContinuityContractError):
        EpisodeOutcome(
            "completed",
            "succeeded",
            "success",
            "passed",
            "not_received",
            assessments={"verification_status": {"author": "host", "verifier": "pytest"}},
        )


def test_a_model_may_report_a_failure_but_never_certify_a_pass():
    EpisodeOutcome(
        "completed",
        "failed",
        "failure",
        "failed",
        "not_received",
        assessments={"verification_status": {"author": "model", "evidence_refs": ["evt-1"]}},
    )
    with pytest.raises(ContinuityContractError):
        EpisodeOutcome(
            "completed",
            "succeeded",
            "success",
            "passed",
            "not_received",
            assessments={"verification_status": {"author": "model", "evidence_refs": ["evt-1"]}},
        )


def test_the_axes_stay_independent_after_the_repair():
    """C5's closing constraint: a failed task must not be forced to imply a broken cascade."""
    outcome = EpisodeOutcome(
        "completed",
        "succeeded",
        "failure",
        "failed",
        "not_received",
        assessments={"verification_status": {"author": "host", "check_refs": ["check-1"]}},
    )
    assert outcome.cascade_status == "completed" and outcome.task_outcome == "failure"


# ── C6 — reload runs the same validators as admission ─────────────────────────


def _stored() -> dict:
    response = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    return response.to_dict()


def test_a_clean_stored_response_still_reloads():
    stored = _stored()
    again = ValidatedWindowResponse.from_dict(stored)
    assert tuple(row["episode_id"] for row in again.reflection["episode_assessments"]) == TEN_MEMBERS


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s["reflection"].__setitem__("episode_assessments", []), id="no_assessments"),
        pytest.param(
            lambda s: s["reflection"]["episode_assessments"].__setitem__(
                1, copy.deepcopy(s["reflection"]["episode_assessments"][0])
            ),
            id="duplicate_assessment",
        ),
        pytest.param(lambda s: s["reflection"]["episode_assessments"].pop(), id="missing_assessment"),
        pytest.param(lambda s: s["reflection"].__setitem__("summary", "x" * 4001), id="oversized_summary"),
        pytest.param(lambda s: s["reflection"]["patterns"][0].pop("falsifier"), id="missing_falsifier"),
        pytest.param(lambda s: s["reflection"].__setitem__("smuggled", 1), id="unknown_nested_field"),
        pytest.param(
            lambda s: s.__setitem__("trajectory_update", {"operation": "continue", "reason": "r"}),
            id="retired_trajectory_word",
        ),
        pytest.param(
            lambda s: s["reflection"]["coverage"].__setitem__("generation_id", "f" * 64),
            id="host_owned_key",
        ),
    ],
)
def test_reload_refuses_what_admission_refuses(mutate):
    stored = _stored()
    mutate(stored)
    with pytest.raises(WindowOutputError):
        ValidatedWindowResponse.from_dict(stored)


def test_a_stored_response_without_coverage_cannot_be_revalidated():
    stored = _stored()
    stored["reflection"]["coverage"]["member_ids"] = []
    with pytest.raises(WindowOutputError):
        ValidatedWindowResponse.from_dict(stored)


def test_the_optional_deltas_survive_a_reload():
    payload = make_valid_response(
        decisions=[{"statement": "seal by finish line", "rationale": "commit order is memory order"}],
        user_constraints=[{"statement": "never ask twice for granted scope"}],
        lane_injections=[
            {
                "target_lane": "agent",
                "objective": "re-run the failing check",
                "constraints": ["do not widen scope"],
                "evidence_refs": ["ev-0001"],
                "requirement": "required",
            }
        ],
    )
    stored = validate_window_response_payload(payload, member_ids=TEN_MEMBERS).to_dict()
    again = ValidatedWindowResponse.from_dict(stored)
    assert len(again.decisions) == 1 and len(again.user_constraints) == 1
    assert again.lane_injections[0]["target_lane"] == "agent"


@pytest.mark.parametrize(
    "key,rows",
    [
        ("decisions", [{"statement": "s"}]),
        ("decisions", [{"statement": "s", "rationale": "r", "extra": 1}]),
        ("user_constraints", [{"note": "not a statement"}]),
        ("lane_injections", [{"target_lane": "agent", "objective": "o"}]),
        ("lane_injections", [
            {
                "target_lane": "nowhere",
                "objective": "o",
                "constraints": [],
                "evidence_refs": [],
                "requirement": "required",
            }
        ]),
        ("lane_injections", [
            {
                "target_lane": "agent",
                "objective": "o",
                "constraints": "not-an-array",
                "evidence_refs": [],
                "requirement": "required",
            }
        ]),
        ("lane_injections", [
            {
                "target_lane": "agent",
                "objective": "o",
                "constraints": [],
                "evidence_refs": [],
                "requirement": "mandatory",
            }
        ]),
    ],
)
def test_a_delta_row_must_meet_its_frozen_contract(key, rows):
    """C6: an outer array type was all the predecessor checked."""
    payload = make_valid_response(**{key: rows})
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(payload, member_ids=TEN_MEMBERS)


def test_the_packet_digest_receipt_survives_a_reload():
    packet = make_packet()
    response = validate_window_response_payload(
        make_valid_response(), member_ids=TEN_MEMBERS, packet_digest=packet.content_sha256()
    )
    again = ValidatedWindowResponse.from_dict(response.to_dict())
    assert again.packet_digest == packet.content_sha256()


def test_structural_validity_is_not_authoritative_binding():
    """The two guarantees C6 separates. A self-declared cohort is not the host's.

    A response can be internally consistent about a cohort the host never sealed, so reload
    proves structure only. WO-13 owns rebinding to the pinned request.
    """
    other = tuple(f"ep-9{n:03d}" for n in range(1, 11))
    stored = validate_window_response_payload(
        make_valid_response(member_ids=other), member_ids=other
    ).to_dict()
    reloaded = ValidatedWindowResponse.from_dict(stored)
    assert tuple(reloaded.reflection["coverage"]["member_ids"]) == other
    assert reloaded.reflection["coverage"]["member_ids"] != list(TEN_MEMBERS)


# ── the fixture corpus still validates after every repair ─────────────────────


def test_every_committed_fixture_still_passes_the_hardened_schemas():
    from tests.helpers.continuity_records import FIXTURE_DIR, load_contract_fixture

    names = sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))
    assert len(names) == 7
    for name in names:
        if name == "conflicting_id_event":
            with pytest.raises(EventIdentityConflict):
                load_contract_fixture(name)
        else:
            assert load_contract_fixture(name) is not None


def test_no_fixture_was_edited_into_a_valid_case_to_pass():
    """The conflicting-ID fixture must still hold two genuinely conflicting presentations."""
    from tests.helpers.continuity_records import FIXTURE_DIR

    body = json.loads((FIXTURE_DIR / "conflicting_id_event.json").read_text(encoding="utf-8"))["body"]
    assert body["first"]["event_id"] == body["second"]["event_id"]
    assert body["first"]["payload"] != body["second"]["payload"]


# ── WO-02A closure: paired constructor / deserializer controls ─────────────────
#
# The closure addendum requires paired controls wherever a SHARED helper changed, so a rule
# cannot hold on one public path and leak on another. Three helpers changed:
# require_string_keys (new), validated_reflection and declared_member_ids (extracted).


def test_evidence_table_keys_are_checked_on_both_public_paths():
    """F1: require_string_keys runs inside __post_init__, so both paths get it."""
    from schema.continuity_json import require_string_keys

    body = make_packet().to_dict()
    row = copy.deepcopy(next(iter(body["evidence_table"].values())))
    body["evidence_table"][2] = row
    with pytest.raises(ContinuityContractError):
        WindowEvidencePacket.from_dict(body)
    with pytest.raises(ContinuityContractError):
        make_packet(evidence_table={**make_packet().evidence_table, 2: row})
    # The helper itself refuses rather than renames.
    with pytest.raises(ContinuityContractError):
        require_string_keys({1: "a"}, where="probe")
    assert require_string_keys({"1": "a"}, where="probe") == {"1": "a"}


def test_a_numeric_string_evidence_key_is_still_legitimate():
    """F1 must not cost the legal case: "1" is a string and stays exactly as given."""
    body = make_packet().to_dict()
    row = copy.deepcopy(next(iter(body["evidence_table"].values())))
    row["value"] = "KEPT"
    body["evidence_table"]["1"] = row
    packet = WindowEvidencePacket.from_dict(body)
    assert packet.to_dict()["evidence_table"]["1"]["value"] == "KEPT"


def test_cascade_status_agreement_holds_on_both_public_paths():
    """F4: the manifest and the capsule both carry cascade_status, so it can disagree."""
    body = make_packet().to_dict()
    body["episodes"][0]["cascade_status"] = "failed"
    with pytest.raises(WindowMembershipError) as excinfo:
        WindowEvidencePacket.from_dict(body)
    assert "cascade_status" in str(excinfo.value) and TEN_MEMBERS[0] in str(excinfo.value)

    manifest = make_window_manifest(member_outcomes={TEN_MEMBERS[0]: "failed"})
    rows = [dict(row) for row in make_packet().episodes]
    with pytest.raises(WindowMembershipError):
        make_packet(manifest=manifest, episodes=rows)


def test_the_four_capsule_only_axes_stay_independent_of_cascade_status():
    """F4 must not force task outcome or verification to follow cascade completion."""
    body = make_packet().to_dict()
    body["episodes"][0]["task_outcome"] = "failure"
    body["episodes"][0]["verification_status"] = "failed"
    body["episodes"][0]["feedback_status"] = "contradicts"
    packet = WindowEvidencePacket.from_dict(body)
    row = packet.episodes[0]
    assert row["cascade_status"] == "completed" and row["task_outcome"] == "failure"


def test_reflection_structure_is_validated_on_construction_admission_and_reload():
    """F3: one shared non-constructing helper, three public paths."""
    from schema.window_reflection import declared_member_ids, validated_reflection

    accepted = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    body = accepted.to_dict()["reflection"]
    assert declared_member_ids(body) == TEN_MEMBERS
    assert validated_reflection(body, member_ids=TEN_MEMBERS)["summary"] == accepted.reflection["summary"]

    broken = copy.deepcopy(body)
    broken["episode_assessments"] = []
    # construction
    with pytest.raises(WindowOutputError):
        replace(accepted, reflection=broken)
    # admission
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(
            {**make_valid_response(), "reflection": broken}, member_ids=TEN_MEMBERS
        )
    # reload
    stored = accepted.to_dict()
    stored["reflection"] = broken
    with pytest.raises(WindowOutputError):
        ValidatedWindowResponse.from_dict(stored)


def _called_names(source: str) -> set[str]:
    """Every function name actually CALLED in *source*, comments and strings excluded."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_construction_does_not_recurse_into_admission():
    """The refactor must not create a construct -> admit -> construct cycle.

    Checked on the parsed call graph rather than by string match: the first version of this
    test matched its own explanatory comment, which would have kept passing after a real
    recursive call was introduced.
    """
    import inspect

    import schema.window_reflection as wr

    post_init_calls = _called_names(inspect.getsource(wr.ValidatedWindowResponse.__post_init__))
    assert "validate_window_response_payload" not in post_init_calls
    assert "validated_reflection" in post_init_calls, "construction must use the shared helper"

    helper_calls = _called_names(inspect.getsource(wr.validated_reflection))
    assert "ValidatedWindowResponse" not in helper_calls
    assert "validate_window_response_payload" not in helper_calls

    # And no second, weaker way in.
    module_source = inspect.getsource(wr)
    for banned in ("_unvalidated", "skip_validation", "validate=False", "unsafe"):
        assert banned not in module_source, banned


@pytest.mark.parametrize("key", ["decisions", "user_constraints", "lane_injections"])
@pytest.mark.parametrize("bad", [False, 0, "", {}, 0.0, "no"], ids=["false", "zero", "empty_str", "empty_map", "zero_float", "str"])
def test_a_malformed_delta_is_refused_identically_on_all_three_paths(key, bad):
    """F2: presence, not truthiness. A falsy malformed value is not absence."""
    raw = make_valid_response()
    raw[key] = copy.deepcopy(bad)
    with pytest.raises(WindowOutputError):
        validate_window_response_payload(raw, member_ids=TEN_MEMBERS)

    accepted = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    stored = accepted.to_dict()
    stored[key] = copy.deepcopy(bad)
    with pytest.raises(WindowOutputError):
        ValidatedWindowResponse.from_dict(stored)

    with pytest.raises(WindowOutputError):
        replace(accepted, **{key: copy.deepcopy(bad)})


@pytest.mark.parametrize("key", ["decisions", "user_constraints", "lane_injections"])
def test_declared_absence_and_an_empty_array_are_both_preserved(key):
    """The three states the truthiness filter used to conflate stay distinct."""
    accepted = validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)
    stored = accepted.to_dict()
    stored[key] = []
    assert ValidatedWindowResponse.from_dict(stored).to_dict()[key] == []

    # Omitted on admission and an explicit null both mean "nothing to say", identically on
    # both paths — the symmetry the F2 repair had to keep.
    raw = make_valid_response()
    raw.pop(key, None)
    assert getattr(validate_window_response_payload(raw, member_ids=TEN_MEMBERS), key) == ()
    raw[key] = None
    assert getattr(validate_window_response_payload(raw, member_ids=TEN_MEMBERS), key) == ()
