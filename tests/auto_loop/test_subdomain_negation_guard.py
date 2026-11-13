"""Fix 1 — negation / hypothetical scope guard for subdomain_for (RED → GREEN).

The grammar is purely lexical and over-fires on negated/hypothetical keyword
carriers ("this is NOT a goal" → goals). The guard widens (returns None) when a
negation token sits within a small window BEFORE the matched keyword, or when
the text is hypothetical/counterfactual — WITHOUT suppressing affirmative
constraint-polarity language (must not / cannot / forbidden) whose trigger word
is itself the category marker, nor negation that follows the trigger.
"""

from __future__ import annotations

from auto_loop.mril_tools.subdomain_helpers import subdomain_for


# ── direct negation of the category term → widen ─────────────────────────
def test_negated_goal_widens():
    assert subdomain_for("this is not a goal, just a stray thought", 3) is None


def test_negated_constraints_widens():
    assert subdomain_for("we have no constraints here at all", 3) is None


def test_negated_open_questions_widens():
    assert subdomain_for("there were no open questions left", 0) is None


def test_negated_face_widens():
    assert subdomain_for("no face was detected in the frame", 7) is None


def test_negated_deploy_widens():
    assert subdomain_for("we did not deploy anything today", 5) is None


def test_negated_traits_widens():
    assert subdomain_for("this is not about my traits at all", 0) is None


def test_negated_milestone_widens():
    assert subdomain_for("we never set any milestone for this", 9) is None


def test_negated_artifacts_widens():
    assert subdomain_for("we don't have any artifacts from this build", 11) is None


# ── hypothetical / counterfactual framing → widen ────────────────────────
def test_hypothetical_imagine_decided_widens():
    assert subdomain_for("imagine if we decided to use a model", 3) is None


def test_hypothetical_suppose_outcome_widens():
    assert subdomain_for("suppose the outcome had been different", 4) is None


# ── REGRESSION: affirmative constraint-polarity language is KEPT ─────────
def test_affirmative_must_not_constraint_kept():
    assert subdomain_for("the model must not write to memory directly", 3) == 2


def test_affirmative_cannot_constraint_kept():
    assert subdomain_for("we cannot exceed eight gigabytes of ram", 3) == 2


def test_affirmative_forbidden_constraint_kept():
    assert subdomain_for("writing to disk during inference is forbidden", 3) == 2


# ── REGRESSION: normal affirmative routing is unaffected ─────────────────
def test_affirmative_goal_kept():
    assert subdomain_for("our goal is to ship deterministic routing", 3) == 0


def test_affirmative_feel_kept():
    assert subdomain_for("how did it feel", 4) == 7


def test_affirmative_rules_kept():
    assert subdomain_for("never do this", 0) == 4


def test_affirmative_first_person_not_negated_kept():
    # "not" follows the subject pronoun → identity is still about the operator
    assert subdomain_for("I am not the cloud, I run locally", 0) == 0


# ── BOUNDARY: 'never' immediately after a content trigger negates the
# event itself → widen (stress-corpus contract, quality slice 2026-07-04;
# supersedes the old keep-the-route rule the battery counted as an
# over-trigger). Softer/distant following negation still keeps the route. ──
def test_never_right_after_trigger_widens():
    assert subdomain_for("the session never actually started", 9) is None


def test_soft_following_negation_keeps_session():
    assert subdomain_for("the session was not recorded", 9) == 0
