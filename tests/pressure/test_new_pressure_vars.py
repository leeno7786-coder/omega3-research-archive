"""New pressure variables (2026-06-30): Omega can RAISE verification + reinforcement pressure,
and the existing ignition layer already routes them to actions. The pressure_surface foldback
lists them; the mind decides whether to raise them (minimal scaffolding)."""

from __future__ import annotations

from omega_loop.episode.prompts import _ALLOWED_PRESSURE_PARAMS, _filter_pressure
from global_loop.intent.run import _map_opened_by_to_intent


def test_new_levers_are_allowed_and_validated():
    assert "verification_debt_delta" in _ALLOWED_PRESSURE_PARAMS
    assert "confidence_debt_delta" in _ALLOWED_PRESSURE_PARAMS
    out = _filter_pressure({"verification_debt_delta": 0.8, "confidence_debt_delta": 0.5})
    assert out == {"verification_debt_delta": 0.8, "confidence_debt_delta": 0.5}


def test_new_types_route_to_existing_intents():
    # verification -> agentic verify pass (USE_SKILL when skill-routing on, else USE_TOOL)
    assert _map_opened_by_to_intent("verification_needed", "") in ("USE_SKILL", "USE_TOOL")
    # reinforcement -> study more
    assert _map_opened_by_to_intent("confidence_reinforcement", "") == "LEARN"
    # operator priority -> the mind's ask
    assert _map_opened_by_to_intent("operator_priority", "") == "ASK_OPERATOR"


def test_unknown_lever_still_dropped():
    # validate-and-drop unchanged: a non-allowed key is dropped.
    assert _filter_pressure({"bogus_debt_delta": 1.0}) == {}


def test_operator_priority_reader_inert_then_fires():
    from pressure_loop import operator_priority_store as store
    from pressure_loop.pressure_field import read_operator_priority

    store.clear()
    assert read_operator_priority(None) == 0.0  # inert by default
    store.set_priority("python")
    assert read_operator_priority(None) > 0.0  # fires once the operator flags a priority
    store.set_priority("python")  # dedup
    assert store.list_priorities() == ["python"]
    store.clear()
    assert read_operator_priority(None) == 0.0


def test_operator_priority_registered_and_typed():
    from pressure_loop.pressure_field import _DEFAULT_READERS, CONTRIBUTOR_OPERATOR_PRIORITY
    from pressure_loop.pressure import _FIELD_ID_TO_TYPE

    assert CONTRIBUTOR_OPERATOR_PRIORITY in _DEFAULT_READERS
    assert _FIELD_ID_TO_TYPE["operator_priority"] == "operator_priority"
