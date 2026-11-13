r"""WO-02 independent negative-contract regressions.

Run from the Omega repository root with its bundled interpreter:
  .\.venv-brain\Scripts\python.exe PATH\test_wo02_review_regressions.py

Uses only pure schema APIs and reads the seven-case contract fixture corpus.
It does not start Omega, write canonical memory, install dependencies, or
modify repository files. The same eight assertions were exercised in memory
against HEAD 3e4033da: eight assertion failures and zero test errors.
"""
# WO-02A: the class is named Test* so tests/conftest.py::_is_pytest_style collects this file
# in a DIRECTORY sweep. Under its original name every assertion still ran when the file was
# named explicitly, but `pytest tests/continuity` silently skipped all eight - which is the
# same silent-empty failure mode these regressions exist to close. Assertions are unchanged.

from __future__ import annotations

import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "continuity" / "fixtures" / "contracts"
if not (ROOT / "schema" / "episode_event.py").is_file() or not FIXTURES.is_dir():
    raise RuntimeError(f"expected the Omega repository root at {ROOT}")
sys.path.insert(0, str(ROOT))

from schema.continuity_json import (  # noqa: E402
    ContinuityContractError,
    EventIdentityConflict,
    Observation,
    parse_json_object,
)
from schema.episode_event import EpisodeEvent  # noqa: E402
from schema.episode_record import EpisodeOutcome  # noqa: E402
from schema.window_evidence import WindowEvidencePacket  # noqa: E402
from schema.window_reflection import (  # noqa: E402
    ValidatedWindowResponse,
    validate_window_response_payload,
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class TestWO02ReviewRegressions(unittest.TestCase):
    def test_numeric_overflow_is_rejected_at_raw_parse(self) -> None:
        with self.assertRaises(ContinuityContractError):
            parse_json_object('{"x":1e9999}')

    def test_snapshot_does_not_coerce_and_collapse_object_keys(self) -> None:
        with self.assertRaises(ContinuityContractError):
            Observation.observed({1: "first", "1": "second"})

    def test_same_event_id_with_changed_sequence_is_a_conflict(self) -> None:
        event = EpisodeEvent.from_dict(fixture("valid_event")["body"])
        changed = replace(event, event_seq=event.event_seq + 1)
        self.assertNotEqual(event.occurrence_id(), changed.occurrence_id())
        with self.assertRaises(EventIdentityConflict):
            event.require_same_occurrence(changed)

    def test_string_is_not_a_reference_array(self) -> None:
        body = fixture("valid_event")["body"]
        body["evidence_refs"] = "abc"
        with self.assertRaises(ContinuityContractError):
            EpisodeEvent.from_dict(body)

    def test_ten_rows_of_one_episode_do_not_cover_ten_members(self) -> None:
        body = fixture("ten_member_packet")["body"]
        body["episodes"] = [copy.deepcopy(body["episodes"][0]) for _ in body["episodes"]]
        with self.assertRaises(ContinuityContractError):
            WindowEvidencePacket.from_dict(body)

    def test_capsule_content_hash_must_match_its_manifest_reference(self) -> None:
        body = fixture("ten_member_packet")["body"]
        original = body["episodes"][0]["ref"]["content_sha256"]
        body["episodes"][0]["ref"]["content_sha256"] = "f" * 64 if original != "f" * 64 else "e" * 64
        with self.assertRaises(ContinuityContractError):
            WindowEvidencePacket.from_dict(body)

    def test_verification_pass_requires_attribution_and_evidence(self) -> None:
        with self.assertRaises(ContinuityContractError):
            EpisodeOutcome("completed", "succeeded", "success", "passed", "not_received")

    def test_saved_response_cannot_reload_without_member_assessments(self) -> None:
        source = fixture("valid_window_result")
        response = validate_window_response_payload(source["body"], member_ids=source["member_ids"])
        stored = response.to_dict()
        stored["reflection"]["episode_assessments"] = []
        with self.assertRaises(ContinuityContractError):
            ValidatedWindowResponse.from_dict(stored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
