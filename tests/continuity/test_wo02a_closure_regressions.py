"""WO-02A closure: remaining C2/C4/C6 boundary gaps. Pure schema tests."""
import copy
from dataclasses import replace

import pytest

from schema.continuity_json import ContinuityContractError
from schema.window_evidence import WindowEvidencePacket
from schema.window_reflection import (
    COGNITIVE_DELTA_KEYS, ValidatedWindowResponse, validate_window_response_payload,
)
from tests.helpers.continuity_records import TEN_MEMBERS, make_packet, make_valid_response


def _accepted_response():
    return validate_window_response_payload(make_valid_response(), member_ids=TEN_MEMBERS)


def test_evidence_table_keys_are_validated_before_coercion():
    body = make_packet().to_dict()
    first = copy.deepcopy(next(iter(body["evidence_table"].values())))
    second = copy.deepcopy(first)
    first["value"] = "FIRST DISTINCT EVIDENCE"
    second["value"] = "SECOND DISTINCT EVIDENCE"
    body["evidence_table"][1] = first
    body["evidence_table"]["1"] = second
    with pytest.raises(ContinuityContractError):
        WindowEvidencePacket.from_dict(body)


@pytest.mark.parametrize("key", COGNITIVE_DELTA_KEYS)
@pytest.mark.parametrize("bad", [False, 0, "", {}], ids=["boolean", "number", "string", "mapping"])
def test_reload_rejects_non_array_deltas_just_as_admission_does(key, bad):
    raw = make_valid_response()
    raw[key] = copy.deepcopy(bad)
    with pytest.raises(ContinuityContractError):
        validate_window_response_payload(raw, member_ids=TEN_MEMBERS)
    stored = _accepted_response().to_dict()
    stored[key] = copy.deepcopy(bad)
    with pytest.raises(ContinuityContractError):
        ValidatedWindowResponse.from_dict(stored)


def test_direct_construction_cannot_bypass_assessment_validation():
    accepted = _accepted_response()
    reflection = accepted.to_dict()["reflection"]
    reflection["episode_assessments"] = []
    with pytest.raises(ContinuityContractError):
        replace(accepted, reflection=reflection)


def test_capsule_cascade_status_cannot_contradict_its_manifest():
    body = make_packet().to_dict()
    original = body["manifest"]["members"][0]["cascade_status"]
    body["episodes"][0]["cascade_status"] = "failed" if original != "failed" else "completed"
    with pytest.raises(ContinuityContractError):
        WindowEvidencePacket.from_dict(body)


def test_string_evidence_key_is_preserved():
    body = make_packet().to_dict()
    row = copy.deepcopy(next(iter(body["evidence_table"].values())))
    row["value"] = "LEGITIMATE STRING KEY"
    body["evidence_table"]["1"] = row
    result = WindowEvidencePacket.from_dict(body)
    assert result.to_dict()["evidence_table"]["1"]["value"] == "LEGITIMATE STRING KEY"


def test_explicit_empty_delta_arrays_survive_reload():
    stored = _accepted_response().to_dict()
    for key in COGNITIVE_DELTA_KEYS:
        stored[key] = []
    result = ValidatedWindowResponse.from_dict(stored).to_dict()
    assert all(result[key] == [] for key in COGNITIVE_DELTA_KEYS)


def test_valid_direct_copy_remains_valid():
    accepted = _accepted_response()
    result = replace(accepted, reflection=accepted.to_dict()["reflection"])
    assert result.to_dict() == accepted.to_dict()


def test_valid_packet_and_response_round_trip_unchanged():
    packet = make_packet()
    assert WindowEvidencePacket.from_dict(packet.to_dict()).to_dict() == packet.to_dict()
    response = _accepted_response()
    assert ValidatedWindowResponse.from_dict(response.to_dict()).to_dict() == response.to_dict()
