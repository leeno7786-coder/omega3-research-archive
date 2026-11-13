"""Regression: DeepSleep consumes Omega's REAL OmegaEpisodeResult.to_dict()
output — where mastery_effects / knowledge_to_ingest are TUPLES (asdict preserves
the field type) — not just hand-built list fixtures. This was the live
first-LEARN persistence bug: Omega computed mastery 0.65 on the episode but the
store stayed 0.0 because the extractors only accepted lists.
"""

import pathlib

from deepsleep_loop.intake.run import build_deepsleep_input_for_episode
from deepsleep_loop.mastery.run import _omega_mastery_effects, update_mastery
from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id
from deepsleep_loop.semantic.run import _omega_knowledge_products
from omega_loop.episode.types import (
    KnowledgeProduct,
    MasteryEffect,
    OmegaEpisodeResult,
)
from tests.core.deepsleep_window_fixtures import producer_outcome_axes


def _omega_result_dict():
    res = OmegaEpisodeResult(
        episode_id="ep",
        cognized=True,
        knowledge_to_ingest=(
            KnowledgeProduct("distributed-systems", "A distributed system passes messages.", 0.9),
        ),
        mastery_effects=(MasteryEffect("distributed-systems", 0.65, 0.1, "grounded"),),
        pressure_params={},
        response_text=None,
        synthesis=None,
        reflection=None,
    )
    return res.to_dict()


def test_to_dict_keeps_tuples():
    od = _omega_result_dict()
    # asdict preserves the field's tuple type — the extractors must accept it.
    assert isinstance(od["mastery_effects"], tuple)
    assert isinstance(od["knowledge_to_ingest"], tuple)


def test_omega_mastery_effects_reads_real_tuple_output():
    effs = _omega_mastery_effects({"omega_result": _omega_result_dict()})
    assert len(effs) == 1
    assert effs[0]["knowledge_mastery"] == 0.65


def test_omega_knowledge_products_reads_real_tuple_output():
    prods = _omega_knowledge_products({"omega_result": _omega_result_dict()})
    assert len(prods) == 1
    assert "messages" in prods[0]["statement"]


def test_update_mastery_persists_from_real_omega_result(tmp_path, monkeypatch):
    mp = tmp_path / "m.json"
    monkeypatch.setattr("pressure_loop.policy.deepsleep_mastery_path", lambda policy=None: mp)
    body = {
        "episode_id": "ep",
        "intent": "LEARN",
        "outcome": {
            "outcome": {
                "status": "completed",
                "execution_outcome": {"executed": True, "status": "success"},
            }
        },
        "outcome_axes": producer_outcome_axes(success=True),
        "execution": {
            "execution_action_pack": {"execution_inputs": {"domain": "distributed-systems"}}
        },
        "omega_result": _omega_result_dict(),
    }
    ds = build_deepsleep_input_for_episode(body, "ep")
    update_mastery(ds, mastery_path=mp)
    rec = load_mastery(mp).get(normalize_domain_id("distributed-systems"))
    assert rec is not None
    assert rec.knowledge_mastery == 0.65
