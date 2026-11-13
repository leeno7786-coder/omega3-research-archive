"""Pressure dedup key alignment with operator queue box."""

from __future__ import annotations

from pressure_loop.contributors import make_contributor
from pressure_loop.pressure import _dedup_key


def test_dedup_key_uses_cycle_id_when_message_empty():
    c1 = make_contributor(
        "operator",
        "operator_query",
        1.0,
        {"message": "", "cycle_id": "c-1"},
    )
    c2 = make_contributor(
        "operator",
        "operator_query",
        1.0,
        {"message": "", "cycle_id": "c-2"},
    )
    assert _dedup_key(c1) != _dedup_key(c2)


def test_dedup_key_matches_box_key_for_same_contributor():
    from pressure_loop.cognition_queue import _box_dedup_key

    c = make_contributor(
        "operator",
        "operator_query",
        1.0,
        {"message": "", "text": "", "cycle_id": "c-align"},
    )
    assert _dedup_key(c) == _box_dedup_key(c)
