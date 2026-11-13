"""WO-02 — strict JSON, missingness, references, and the EpisodeEvent contract.

Every assertion here is about a rule CONTRACTS.md states, and every rule exists because a
permissive version of it has already cost this codebase a measured defect:

* duplicate-key erasure — `omega_loop/episode/run.py` parses model output with
  `json.loads` (last-wins) and an `ast.literal_eval` fallback BEFORE any strict validator
  sees a dict, so a second `"status"` silently wins (WO-01 loss L10);
* "could not run" sharing a return value with "ran, found nothing" — AGENTS.md section 2.1,
  the most expensive defect class in this repository;
* a provider naming its own evidence class — an external string must not be able to call
  itself a commit receipt.
"""

from __future__ import annotations

import math

import pytest

from schema.continuity_json import (
    CanonicalRef,
    ContinuityContractError,
    EventIdentityConflict,
    Observation,
    OBSERVATION_STATES,
    SourceRef,
    canonical_bytes,
    parse_json_object,
)
from schema.episode_event import (
    EPISODE_PHASES,
    EVIDENCE_CLASSES,
    EpisodeEvent,
)
from tests.helpers.continuity_records import make_event


# ── strict JSON input ────────────────────────────────────────────────────────


def test_duplicate_keys_cannot_erase_an_earlier_status():
    """The RED seed from the order. A permissive parse resolves this last-wins."""
    with pytest.raises(ContinuityContractError):
        parse_json_object('{"status":"failed","status":"succeeded"}')


def test_duplicate_keys_are_rejected_at_any_nesting_depth():
    with pytest.raises(ContinuityContractError):
        parse_json_object('{"payload":{"verdict":"failed","verdict":"passed"}}')


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_rejected(raw):
    with pytest.raises(ContinuityContractError):
        parse_json_object('{"elapsed":' + raw + "}")


@pytest.mark.parametrize("raw", ["[]", '"text"', "7", "true", "null"])
def test_a_non_object_root_is_rejected(raw):
    with pytest.raises(ContinuityContractError):
        parse_json_object(raw)


def test_trailing_material_after_the_object_is_rejected():
    with pytest.raises(ContinuityContractError):
        parse_json_object('{"a":1} {"b":2}')


def test_a_python_literal_is_not_json():
    """`ast.literal_eval` acceptance is exactly what the live extractor falls back to."""
    with pytest.raises(ContinuityContractError):
        parse_json_object("{'a': 1}")


def test_malformed_json_names_its_own_failure():
    with pytest.raises(ContinuityContractError) as excinfo:
        parse_json_object('{"a":')
    assert "json" in str(excinfo.value).lower()


def test_a_valid_object_round_trips_unchanged():
    assert parse_json_object('{"b":2,"a":[1,2,{"c":null}]}') == {"a": [1, 2, {"c": None}], "b": 2}


# ── canonical bytes ──────────────────────────────────────────────────────────


def test_insertion_order_permutations_give_identical_canonical_bytes():
    first = canonical_bytes({"b": 1, "a": {"y": 2, "x": 3}})
    second = canonical_bytes({"a": {"x": 3, "y": 2}, "b": 1})
    assert first == second == b'{"a":{"x":3,"y":2},"b":1}'


def test_a_reordered_sequence_changes_canonical_bytes():
    assert canonical_bytes({"members": ["a", "b"]}) != canonical_bytes({"members": ["b", "a"]})


def test_canonical_bytes_are_utf8_and_not_escaped():
    assert canonical_bytes({"t": "café — ok"}) == '{"t":"café — ok"}'.encode("utf-8")


def test_canonical_bytes_carry_no_trailing_newline():
    assert not canonical_bytes({"a": 1}).endswith(b"\n")


def test_canonical_bytes_refuse_a_non_finite_number():
    with pytest.raises(ContinuityContractError):
        canonical_bytes({"a": math.inf})


def test_canonical_bytes_refuse_an_object_they_would_have_to_stringify():
    """`default=str` is how evidence silently becomes a repr."""
    with pytest.raises(ContinuityContractError):
        canonical_bytes({"when": object()})


def test_boolean_is_not_an_integer_in_canonical_bytes():
    assert canonical_bytes({"n": True}) != canonical_bytes({"n": 1})


# ── Observation ──────────────────────────────────────────────────────────────


def test_every_observation_state_is_declared():
    assert OBSERVATION_STATES == (
        "observed",
        "unknown",
        "not_applicable",
        "unavailable",
        "redacted",
        "not_captured",
    )


def test_an_observed_observation_may_carry_explicitly_empty_content():
    empty = Observation.observed("")
    assert empty.state == "observed" and empty.value == "" and empty.reason == ""
    assert Observation.observed(0).value == 0
    # Sequences are frozen to tuples on the way in, for immutability, and restored to JSON
    # arrays on the way out. An explicitly empty array survives both directions.
    assert Observation.observed([]).value == ()
    assert Observation.observed([]).to_dict()["value"] == []


@pytest.mark.parametrize("state", ["unknown", "not_applicable", "unavailable", "redacted", "not_captured"])
def test_a_non_observed_observation_needs_a_reason_and_no_value(state):
    missing = Observation.missing(state, reason="the producer did not record it")
    assert missing.value is None and missing.reason
    with pytest.raises(ContinuityContractError):
        Observation(state=state, value="smuggled", reason="has a reason but also a value")
    with pytest.raises(ContinuityContractError):
        Observation(state=state, value=None, reason="   ")


def test_an_unknown_state_name_is_rejected():
    with pytest.raises(ContinuityContractError):
        Observation(state="failed", value=None, reason="historical absence is not a failure")


def test_observed_empty_and_unknown_are_different_values():
    assert Observation.observed("") != Observation.missing("unknown", reason="not recorded")


def test_an_observation_round_trips_through_its_own_dict_form():
    for observation in (
        Observation.observed("2026-09-20T00:00:00Z"),
        Observation.observed(""),
        Observation.missing("not_captured", reason="pre-WO-03 record"),
    ):
        assert Observation.from_dict(observation.to_dict()) == observation


def test_an_observation_rejects_unknown_keys():
    with pytest.raises(ContinuityContractError):
        Observation.from_dict({"state": "observed", "value": "x", "reason": "", "extra": 1})


# ── references ───────────────────────────────────────────────────────────────


def test_a_canonical_ref_requires_a_full_content_hash():
    ref = CanonicalRef(record_id="9.01.aaaa1111", record_version=3, content_sha256="a" * 64)
    assert ref.record_version == 3
    with pytest.raises(ContinuityContractError):
        CanonicalRef(record_id="9.01.aaaa1111", record_version=1, content_sha256="abc")


@pytest.mark.parametrize(
    "path",
    ["J:/Omega3.0/memory/x.json", "/etc/passwd", "C:\\Users\\someone\\x.json", "../../secrets.json", "a/../../b.json"],
)
def test_a_canonical_ref_rejects_an_absolute_or_traversing_locator(path):
    with pytest.raises(ContinuityContractError):
        CanonicalRef(
            record_id="9.01.aaaa1111",
            record_version=1,
            content_sha256="b" * 64,
            relative_path=path,
        )


def test_a_canonical_ref_accepts_a_profile_relative_locator():
    ref = CanonicalRef(
        record_id="9.01.aaaa1111",
        record_version=1,
        content_sha256="c" * 64,
        relative_path="memory/900_temporal/901_events/9.01.aaaa1111.json",
    )
    assert ref.relative_path.endswith("9.01.aaaa1111.json")


def test_a_canonical_ref_rejects_a_boolean_version():
    with pytest.raises(ContinuityContractError):
        CanonicalRef(record_id="9.01.aaaa1111", record_version=True, content_sha256="d" * 64)


def test_a_source_ref_does_not_claim_canonical_publication():
    source = SourceRef(source_id="src-0001", source_type="operator_chat", revision="rev-1")
    assert not hasattr(source, "record_version")
    assert "content_sha256" not in source.to_dict()


# ── EpisodeEvent ─────────────────────────────────────────────────────────────


def test_the_phase_vocabulary_is_the_cascade_and_admission():
    assert EPISODE_PHASES == (
        "admission",
        "pressure",
        "global_a",
        "meta_a",
        "auto",
        "meta_b",
        "global_b",
        "omega",
        "deepsleep",
    )


def test_the_evidence_classes_separate_host_operator_external_model_verification_and_receipt():
    assert EVIDENCE_CLASSES == (
        "host_observation",
        "operator_statement",
        "external_content",
        "model_conclusion",
        "verification_result",
        "commit_receipt",
    )


def test_a_valid_event_exposes_every_required_group():
    event = make_event()
    as_dict = event.to_dict()
    for key in (
        "schema_version",
        "event_id",
        "episode_id",
        "thread_id",
        "event_seq",
        "operation_id",
        "phase_attempt",
        "phase",
        "event_kind",
        "observed_at",
        "occurred_at",
        "boot_id",
        "elapsed_ns",
        "producer",
        "evidence_class",
        "status",
        "payload",
        "evidence_refs",
        "artifact_refs",
    ):
        assert key in as_dict, key


def test_an_event_round_trips_through_strict_parsing():
    event = make_event()
    assert EpisodeEvent.from_dict(parse_json_object(canonical_bytes(event.to_dict()).decode())) == event


def test_an_event_rejects_an_unknown_field():
    payload = make_event().to_dict()
    payload["confidence"] = 0.9
    with pytest.raises(ContinuityContractError):
        EpisodeEvent.from_dict(payload)


def test_an_event_rejects_an_unknown_phase():
    with pytest.raises(ContinuityContractError):
        make_event(phase="reflection")


def test_an_event_sequence_is_positive_and_per_episode():
    with pytest.raises(ContinuityContractError):
        make_event(event_seq=0)
    with pytest.raises(ContinuityContractError):
        make_event(event_seq=True)


def test_external_content_cannot_call_itself_a_commit_receipt():
    """A provider's own text may not assign itself the receipt class."""
    with pytest.raises(ContinuityContractError):
        make_event(
            evidence_class="commit_receipt",
            producer={"component": "connectors.gmail", "component_version": "1", "authority": "external"},
        )


def test_a_commit_receipt_requires_host_authority():
    event = make_event(
        evidence_class="commit_receipt",
        producer={"component": "deepsleep_loop.loop", "component_version": "1", "authority": "host"},
    )
    assert event.evidence_class == "commit_receipt"


def test_occurred_at_and_elapsed_are_observations_not_bare_values():
    event = make_event()
    assert isinstance(event.occurred_at, Observation)
    assert isinstance(event.elapsed_ns, Observation)
    with pytest.raises(ContinuityContractError):
        make_event(occurred_at="2026-09-20T00:00:00Z")


def test_a_historically_unrecorded_time_is_not_captured_rather_than_failed():
    event = make_event(occurred_at=Observation.missing("not_captured", reason="pre-WO-03 journal row"))
    assert event.occurred_at.state == "not_captured"
    assert event.occurred_at.value is None


def test_an_absent_optional_identity_is_omitted_rather_than_invented():
    event = make_event()
    as_dict = event.to_dict()
    for optional in ("turn_id", "continuation_id", "job_id", "attempt_id", "arrival_seq", "pipeline_id"):
        assert optional not in as_dict


def test_an_optional_identity_survives_when_the_host_recorded_it():
    event = make_event(arrival_seq=41, pipeline_id=2, turn_id="turn-0001")
    as_dict = event.to_dict()
    assert as_dict["arrival_seq"] == 41 and as_dict["pipeline_id"] == 2
    assert EpisodeEvent.from_dict(as_dict) == event


def test_arrival_seq_is_a_positive_integer_or_the_absent_sentinel():
    """0 means the host did not record one, matching CognitiveEpisode's own -1 idiom.

    It is omitted from serialization rather than written as zero, so a reader can never
    mistake "not recorded" for "arrived first".
    """
    assert "arrival_seq" not in make_event(arrival_seq=0).to_dict()
    with pytest.raises(ContinuityContractError):
        make_event(arrival_seq=-1)
    with pytest.raises(ContinuityContractError):
        make_event(arrival_seq=True)


def test_mutating_the_input_payload_cannot_mutate_an_accepted_event():
    payload = {"verdict": "passed", "checks": [{"name": "ruff"}]}
    event = make_event(payload=payload)
    payload["verdict"] = "failed"
    payload["checks"][0]["name"] = "tampered"
    assert event.payload["verdict"] == "passed"
    assert event.payload["checks"][0]["name"] == "ruff"


def test_an_accepted_event_cannot_be_mutated_through_its_own_payload():
    event = make_event()
    with pytest.raises(Exception):
        event.payload["injected"] = True


def test_two_real_operations_with_equal_payload_bytes_are_different_events():
    first = make_event(operation_id="op-0001", event_seq=1)
    second = make_event(operation_id="op-0002", event_seq=2)
    assert first.payload == second.payload
    assert first.occurrence_id() != second.occurrence_id()


def test_content_hash_is_separate_from_occurrence_identity():
    first = make_event(operation_id="op-0001", event_seq=1)
    second = make_event(operation_id="op-0002", event_seq=2)
    assert first.content_sha256() == second.content_sha256()


def test_the_same_event_id_with_conflicting_content_is_an_identity_conflict():
    first = make_event(event_id="evt-0001", payload={"verdict": "passed"})
    second = make_event(event_id="evt-0001", payload={"verdict": "failed"})
    with pytest.raises(EventIdentityConflict):
        first.require_same_occurrence(second)


def test_the_same_event_presented_again_unchanged_is_not_a_conflict():
    first = make_event(event_id="evt-0001")
    second = make_event(event_id="evt-0001")
    assert first.require_same_occurrence(second) is first


def test_evidence_and_artifact_refs_are_ordered_and_bounded():
    event = make_event(evidence_refs=("b", "a"), artifact_refs=("y",))
    assert event.evidence_refs == ("b", "a")
    with pytest.raises(ContinuityContractError):
        make_event(evidence_refs=tuple(f"ref-{n}" for n in range(64)))


def test_a_producer_must_name_its_component_and_version():
    with pytest.raises(ContinuityContractError):
        make_event(producer={"component": "auto_loop.execute"})
    with pytest.raises(ContinuityContractError):
        make_event(producer={"component": "", "component_version": "1", "authority": "host"})


def test_a_producer_may_record_model_prompt_tool_and_config_revisions():
    event = make_event(
        producer={
            "component": "omega_loop.episode.run",
            "component_version": "wo02",
            "authority": "host",
            "model": "synthetic-model-a",
            "prompt_revision": "meta_omega.window@1",
            "tool_revision": "brain_skill_executor@1",
            "config_revision": "reflection_window@1",
            "verification_revision": "acceptance@1",
        }
    )
    assert event.producer["model"] == "synthetic-model-a"


def test_a_producer_rejects_an_unknown_revision_field():
    with pytest.raises(ContinuityContractError):
        make_event(
            producer={
                "component": "x",
                "component_version": "1",
                "authority": "host",
                "temperature": "0",
            }
        )
