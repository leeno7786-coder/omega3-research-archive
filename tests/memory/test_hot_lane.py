"""HOT lane — in-session turns surface through recall, scored first.

The hot lane runs over the live session buffer (memory.hot_buffer),
not the coord index: hits carry their text inline in metadata (no
canonical file to load), the just-asked question never matches itself,
and the lane defaults ON for every posture — the present conversation
is always in scope unless a posture explicitly silences it with
``hot: 0.0``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import memory.hot_buffer as hb  # noqa: E402
from memory.recall_compiler import compile_recall_plan  # noqa: E402
from memory.recall_lanes import (  # noqa: E402
    LANE_HOT,
    ALL_LANE_NAMES,
    run_recall_lanes,
)


class FakeQS:
    def __init__(self, text: str, limit: int = 10):
        self.cycle_id = "test-hot-lane"
        self.text = text
        self.limit = limit
        self.modality = "text"
        self.task_scope = None
        self.recall_policy_id = None
        self.domain = None


@pytest.fixture(autouse=True)
def _fresh_buffer():
    hb._reset_for_tests()
    yield
    hb._reset_for_tests()


def _hits_for(question: str, plan=None):
    plan = plan or compile_recall_plan(FakeQS(question))
    return [h for h in run_recall_lanes(plan, {}, lambda path: None) if h.lane == LANE_HOT]


def test_hot_is_a_registered_lane():
    assert LANE_HOT == "hot"
    assert LANE_HOT in ALL_LANE_NAMES


def test_hot_lane_surfaces_session_turns():
    hb.begin_session("chat-7")
    hb.append_turn("user", "I adopted a kitten named Miso yesterday")
    hb.append_turn("assistant", "Miso — noted!")
    hits = _hits_for("what did I name the kitten")
    assert hits, "expected hot-lane hits from the live session"
    top = hits[0]
    assert "Miso" in (top.metadata or {}).get("text", "")
    assert top.memory_id.startswith("hot:chat-7:")
    assert 0.0 < top.score <= 1.0


def test_hot_lane_empty_without_session():
    assert _hits_for("anything at all here") == []


def test_hot_lane_does_not_match_the_question_itself():
    hb.begin_session("chat-8")
    hb.append_turn("user", "what did I name the kitten")  # the ingress echo
    hits = _hits_for("what did I name the kitten")
    assert hits == [], "the just-asked question must not answer itself"


def test_hot_lane_respects_explicit_zero_weight():
    from types import SimpleNamespace

    hb.begin_session("chat-9")
    hb.append_turn("user", "the project deadline moved to Friday")
    real = compile_recall_plan(FakeQS("when is the project deadline"))
    weights = dict(getattr(real, "lane_weights", {}) or {})
    weights["hot"] = 0.0
    plan = SimpleNamespace(
        lane_weights=weights,
        limit_per_lane=getattr(real, "limit_per_lane", 10),
        query_text="when is the project deadline",
        exact_terms=getattr(real, "exact_terms", []),
        expanded_terms=getattr(real, "expanded_terms", []),
        posture=getattr(real, "posture", None),
    )
    hits = [h for h in run_recall_lanes(plan, {}, lambda path: None) if h.lane == LANE_HOT]
    assert hits == [], "hot: 0.0 must suppress the lane"


def test_hot_hits_carry_inline_metadata():
    hb.begin_session("chat-10")
    hb.append_turn("user", "my sister June visits on Sunday")
    hits = _hits_for("when does my sister June visit")
    assert hits
    md = hits[0].metadata or {}
    assert md.get("role") == "user"
    assert md.get("session_id") == "chat-10"
    assert "June" in md.get("text", "")
