"""Fix 2 — per-lobe synonym / vocabulary expansion (RED → GREEN).

Paraphrases of one intent must land on the same subdomain even when a synonym
the grammar didn't know is used (forbidden, settled on, accomplished,
deliverable, visage, annoyed, "in the end"), and recall phrasings must reach
the write target (characteristics ≡ traits, "who is the operator" ≡ constitution).
All synonyms route through the negation guard, so a negated synonym still widens.
"""

from __future__ import annotations

from auto_loop.mril_tools.subdomain_helpers import subdomain_for


# ── paraphrase synonyms land on the group's target Y ─────────────────────
def test_goal_synonym_intend():
    assert subdomain_for("we intend to ship deterministic routing", 3) == 0


def test_goal_synonym_want():
    assert subdomain_for("what we want is fully deterministic routing", 3) == 0


def test_decision_synonym_settled():
    assert subdomain_for("we settled on retiring the classifier", 3) == 1


def test_guardian_synonym_forbidden():
    assert subdomain_for("deleting operator memories is forbidden", 0) == 4


def test_sentiment_synonym_annoyed():
    assert subdomain_for("debugging left me annoyed and drained", 4) == 7


def test_face_synonym_visage():
    assert subdomain_for("log the visage from the webcam", 7) == 1


def test_outcome_synonym_in_the_end():
    assert subdomain_for("in the end we rolled back", 4) == 6


def test_milestone_synonym_accomplished():
    assert subdomain_for("we accomplished full offline this week", 9) == 2


def test_artifact_synonym_deliverable():
    assert subdomain_for("the build generated a deliverable package", 11) == 0


# ── findability synonyms (recall phrasing reaches the write target) ──────
def test_traits_synonym_characteristics():
    assert subdomain_for("what personality characteristics do I have", 0) == 2


def test_constitution_operator_keyword():
    assert subdomain_for("who is the operator", 0) == 0


# ── REGRESSION: synonyms still obey the negation guard ───────────────────
def test_negated_synonym_settle_widens():
    assert subdomain_for("we did not settle on anything yet", 3) is None


def test_negated_synonym_deliverable_widens():
    assert subdomain_for("there is no deliverable yet", 11) is None


# ── REGRESSION: existing affirmative routes unaffected ───────────────────
def test_existing_goal_kept():
    assert subdomain_for("our goal is to ship routing", 3) == 0


def test_existing_face_kept():
    assert subdomain_for("recognize this face", 7) == 1
