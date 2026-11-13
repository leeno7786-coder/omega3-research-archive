"""§7-P1-3(b): validate_config_document REJECTS malformed governed docs.

`config/schema_validate.py::validate_config_document` runs on every governed
config load (via `config/loader._load_yaml(..., schema_stem=...)`). The
existing suite pins that the *live* configs validate, but nothing pinned the
fail-mode: that a document *violating* a stem's JSON Schema is REJECTED with a
``ValueError``. This file pins both directions of that gate.

Governed stem under test: ``reflection_window`` (schema/reflection_window.schema.yaml).
Its ``ReflectionWindowConfig`` definition:
  - requires top-level key ``reflection_window`` (object)
  - that object requires ``window_size`` (integer, minimum 1)

So we have four distinct, schema-derived violation modes plus one known-good
shape. ``cognition_chain`` is included as a second stem so the gate is pinned
as a general property of ``validate_config_document``, not a quirk of one
schema.
"""

from __future__ import annotations

import pytest

from config import loader
from config.schema_validate import validate_config_document


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    """Isolate from any cached governed-config reads."""
    loader.clear_cache()
    yield
    loader.clear_cache()


# ---------------------------------------------------------------------------
# Direction 1 — a VALID document PASSES (no raise).
# ---------------------------------------------------------------------------


def test_valid_reflection_window_document_passes():
    """A document satisfying the schema must NOT raise."""
    valid = {"reflection_window": {"window_size": 10}}
    # Must return None and raise nothing.
    assert validate_config_document("reflection_window", valid) is None


def test_live_reflection_window_config_passes():
    """The on-disk governed config is the canonical valid document and
    must pass — anchors the valid direction to a real, shipped doc."""
    live = loader._load_governed_config("reflection_window")
    # window_size is the pinned constraint; assert the live VALUE satisfies it.
    assert live["reflection_window"]["window_size"] >= 1
    assert validate_config_document("reflection_window", live) is None


# ---------------------------------------------------------------------------
# Direction 2 — MALFORMED documents are REJECTED (raise ValueError).
# Each case violates a specific, schema-declared constraint.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_doc", "expected_path_fragment"),
    [
        # window_size below the declared minimum of 1.
        ({"reflection_window": {"window_size": 0}}, "window_size"),
        # window_size present but wrong type (string, not integer).
        ({"reflection_window": {"window_size": "ten"}}, "window_size"),
        # required nested key window_size absent.
        ({"reflection_window": {}}, "reflection_window"),
        # required top-level key reflection_window absent.
        ({}, "<root>"),
    ],
    ids=[
        "window_size_below_minimum",
        "window_size_wrong_type",
        "missing_window_size",
        "missing_reflection_window_root",
    ],
)
def test_malformed_reflection_window_document_is_rejected(bad_doc, expected_path_fragment):
    """Each schema-violating document must raise ValueError naming the
    offending path — pins the FAIL-MODE, not mere 'something raised'."""
    with pytest.raises(ValueError, match="schema violation") as exc_info:
        validate_config_document("reflection_window", bad_doc)
    # The error must localize the violation to the offending field/path.
    assert expected_path_fragment in str(exc_info.value)


def test_window_size_minimum_is_a_two_sided_gate():
    """Pin BOTH sides of the minimum-of-1 boundary on the SAME field:
    window_size == 1 (the minimum) is accepted; window_size == 0 is rejected.
    A presence-only test would pass even if the floor were mis-set."""
    # At the floor: accepted.
    assert (
        validate_config_document("reflection_window", {"reflection_window": {"window_size": 1}})
        is None
    )
    # One below the floor: rejected.
    with pytest.raises(ValueError, match="schema violation"):
        validate_config_document("reflection_window", {"reflection_window": {"window_size": 0}})


# ---------------------------------------------------------------------------
# Second governed stem — generality of the gate.
# cognition_chain requires cognition_chain.{enabled, cascade}; cascade items
# must be the CascadeStep enum.
# ---------------------------------------------------------------------------


def test_valid_cognition_chain_document_passes():
    valid = {
        "cognition_chain": {
            "motor_programs": {
                "enabled": True,
                "shadow_mode": False,
                "eligible_actions": ["USE_TOOL"],
                "max_parse_attempts": 1,
                "timeout_seconds": 60,
                "equivalent_no_progress_limit": 1,
                "distinct_failure_limit": 1,
            },
            "fetched_comprehension": {"timeout_seconds": 60},
        }
    }
    assert validate_config_document("cognition_chain", valid) is None


@pytest.mark.parametrize(
    ("bad_doc", "expected_path_fragment"),
    [
        # enabled must be boolean, not a string.
        (
            {"cognition_chain": {"enabled": "yes", "cascade": []}},
            "enabled",
        ),
        # cascade item not in the CascadeStep enum.
        (
            {"cognition_chain": {"enabled": True, "cascade": ["not_a_step"]}},
            "cascade",
        ),
        # required key cascade absent.
        ({"cognition_chain": {"enabled": True}}, "cognition_chain"),
    ],
    ids=[
        "enabled_wrong_type",
        "cascade_invalid_enum_member",
        "missing_cascade",
    ],
)
def test_malformed_cognition_chain_document_is_rejected(bad_doc, expected_path_fragment):
    with pytest.raises(ValueError, match="schema violation") as exc_info:
        validate_config_document("cognition_chain", bad_doc)
    assert expected_path_fragment in str(exc_info.value)
