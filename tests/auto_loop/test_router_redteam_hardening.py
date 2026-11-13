"""Red-team hardening (post multi-angle red-team) — RED → GREEN.

Fixes from the live-router red-team:
  1. "never" as a plain adverb must NOT misfile into the guardian safety
     subdomain when a third party is the subject ("he never told me his name").
  2. A leading specific recall intent must beat a trailing "what do you know
     about ..." clause.
  3. code/workflow markers must match on word boundaries — "frustrated" must not
     match the "rust" marker and route a mood memory to the CODE lobe.
  4. recall anchors tolerate adverb insertion, plurals, and the "going with"
     gerund; the persona "named" anchor isn't brittle to extra words.
"""

from __future__ import annotations

from auto_loop.mril_tools.router import route_memory_tool
from auto_loop.mril_tools.subdomain_helpers import subdomain_for


def _lobe(q: str) -> int:
    return route_memory_tool(q)["lobe"]


# ── Fix 1: "never" must not steal third-person identity into guardian ─────
def test_third_person_never_is_personality_not_guardian():
    assert subdomain_for("he never told me his real name", 0) == 1


def test_third_party_never_quote_is_personality():
    assert subdomain_for("she said she would never sign off on a risky release", 0) == 1


def test_imperative_never_still_guardian():
    assert subdomain_for("never do this", 0) == 4


def test_must_never_still_guardian():
    assert subdomain_for("you must never delete operator memories", 0) == 4


def test_strong_rule_marker_still_guardian():
    assert subdomain_for("privacy is non-negotiable", 0) == 4


# ── Fix 2: leading specific recall intent beats trailing "know about" ─────
def test_leading_goals_beats_trailing_know_about():
    assert _lobe("what goals did we set and what do you know about the deadline") == 3


def test_what_do_you_know_about_still_knowledge():
    assert _lobe("what do you know about black holes") == 2


def test_know_about_events_stays_knowledge():
    # "what events" is not adjacent here, so the category intent must not fire
    assert _lobe("what do you know about the events of world war two") == 2


# ── Fix 3: code/workflow markers match on word boundaries ────────────────
def test_frustrated_mood_not_code_lobe():
    assert _lobe("log my mood as frustrated") != 8


def test_genuine_rust_still_code():
    assert _lobe("notes on the rust borrow checker and its lifetime rules") == 8


# ── Fix 4: recall anchor robustness ──────────────────────────────────────
def test_persona_recall_tolerates_adverb():
    assert _lobe("what do you actually remember about me") == 1


def test_recall_plural_goals():
    assert _lobe("what are our goals for this quarter") == 3


def test_decisions_going_with_gerund():
    assert subdomain_for("we are going with grammar routing", 3) == 1


def test_persona_who_named_loose():
    assert _lobe("who is that guy named in the notes") == 1
