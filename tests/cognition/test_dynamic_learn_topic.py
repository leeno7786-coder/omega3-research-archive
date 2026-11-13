"""Dynamic learning topics — Omega chooses WHAT to learn, Meta chooses WHERE.

Operator directive 2026-07-08: "make meta smarter so when omega picks learn, it
also adds the topic to learn to it and then meta can choose the best api to
resolve it too" + "make it work for any learning scenario, awaken, user
directed, onboarding etc."

The mechanism is GENERAL:
  - Omega names a SPECIFIC sub-topic on any LEARN decision (awakening sets the
    stash directly; every per-episode intent_decision that names a topic
    captures it via the cognition_chain shadow-write).
  - The LEARN topic resolver consumes that specific choice (above the
    domain-based resolvers) so study advances instead of re-fetching the domain
    overview.
  - Meta A picks the best API for the resolved topic (research -> arXiv).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cognition.topic_resolution.resolve_topic as rt  # noqa: E402
from pressure_loop import learn_topic_choice as ltc  # noqa: E402


class _State:
    meta = {}
    mi = None
    target_str = ""
    text = ""
    episode_id = "ep-dyn"


@pytest.fixture(autouse=True)
def _fresh():
    ltc._reset_for_tests()
    yield
    ltc._reset_for_tests()


# --- the one-shot stash -------------------------------------------------------


def test_stash_set_and_take_is_one_shot():
    ltc.set_chosen_topic("algorithms", "Dijkstra shortest-path")
    assert ltc.peek_chosen_topic() == {"domain": "algorithms", "topic": "Dijkstra shortest-path"}
    assert ltc.take_chosen_topic() == {"domain": "algorithms", "topic": "Dijkstra shortest-path"}
    assert ltc.take_chosen_topic() is None  # consumed


def test_stash_ignores_empty():
    ltc.set_chosen_topic("", "x")
    ltc.set_chosen_topic("d", "")
    assert ltc.peek_chosen_topic() is None


# --- the resolver: Omega's specific choice drives the LEARN topic -------------


def test_chosen_topic_resolves_as_learn_topic():
    ltc.set_chosen_topic("algorithms", "Dijkstra shortest-path algorithm")
    resolved = rt._resolve_omega_chosen_topic(_State())
    assert resolved is not None
    assert resolved.topic == "Dijkstra shortest-path algorithm"
    assert resolved.source == "omega_choice"
    # one-shot: consumed, so a residual re-ignition falls through
    assert rt._resolve_omega_chosen_topic(_State()) is None


def test_bare_domain_echo_yields_to_cascade():
    # a "specific" topic that is just the domain adds nothing -> fall through
    ltc.set_chosen_topic("algorithms", "algorithms")
    assert rt._resolve_omega_chosen_topic(_State()) is None


def test_chosen_topic_sits_above_domain_resolvers(monkeypatch):
    # the specific choice beats the domain-based cascade
    ltc.set_chosen_topic("deep-learning", "backpropagation through time")
    resolved = rt.resolve_learning_topic(_State())
    assert resolved.topic == "backpropagation through time"
    assert resolved.source == "omega_choice"


def test_operator_query_still_wins_over_chosen(monkeypatch):
    # user-directed LEARN keeps priority (their words ARE the specific topic)
    ltc.set_chosen_topic("algorithms", "quicksort")
    st = _State()
    st.target_str = "learn about red-black trees"
    monkeypatch.setattr(rt, "_is_operator_origin", lambda s: True)
    resolved = rt.resolve_learning_topic(st)
    assert resolved.source == "operator_query"
    assert "red-black" in resolved.topic.lower()


# --- Meta A best-API routing (general, keyed on the topic) --------------------


def test_meta_routes_research_topic_by_mastery_level(monkeypatch):
    """Operator 2026-07-11: novices get the encyclopedia, experts get papers.
    arXiv unlocks only when the domain's mastery leaves the 'learning' band;
    a day-one domain (no mastery record) routes to the encyclopedic path."""
    from types import SimpleNamespace

    from deepsleep_loop.mastery_store import normalize_domain_id
    from meta_loop.tool.run import _best_learning_source

    # Day-one novice: no mastery record at all -> encyclopedic default.
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda: {})
    assert _best_learning_source("backpropagation in neural networks", "deep-learning") is None

    # Still in the 'learning' band -> encyclopedic, despite the arXiv topic hints.
    novice = {normalize_domain_id("deep-learning"): SimpleNamespace(knowledge_mastery=0.2)}
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda: novice)
    assert _best_learning_source("backpropagation in neural networks", "deep-learning") is None

    # Past the novice band -> research papers earned.
    operational = {normalize_domain_id("deep-learning"): SimpleNamespace(knowledge_mastery=0.7)}
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda: operational)
    assert _best_learning_source("backpropagation in neural networks", "deep-learning") == "arxiv"


def test_meta_routes_general_topic_to_default():
    from meta_loop.tool.run import _best_learning_source

    assert _best_learning_source("French cooking techniques", "cooking") is None
    assert _best_learning_source("history of the Roman empire", "history") is None


# --- per-episode capture: intent_decision.topic -> stash (GENERAL path) -------


def test_intent_decision_carries_topic():
    from omega_loop.episode.prompts import _filter_intent_decision

    d = _filter_intent_decision(
        {
            "intent": "LEARN",
            "domain": "algorithms",
            "topic": "A* search heuristics",
            "confidence": 0.8,
            "reason": "advance",
        }
    )
    assert d["topic"] == "A* search heuristics"


def test_intent_decision_topic_optional():
    from omega_loop.episode.prompts import _filter_intent_decision

    d = _filter_intent_decision({"intent": "LEARN", "domain": "algorithms"})
    assert d["topic"] is None
