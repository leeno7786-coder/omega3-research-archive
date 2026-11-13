"""Battery-1000 backlog fixes (2026-07-04) — RED cases lifted verbatim from the
official out-of-corpus run (scripts/memory_battery_1000.py). Every fix is a
general rule; both directions pinned.
"""

from __future__ import annotations

from auto_loop.mril_tools.router import route_memory_tool
from auto_loop.mril_tools.subdomain_helpers import subdomain_for


def _lobe(q):
    return route_memory_tool(q)["lobe"]


class TestPositiveObligationRules:
    # 12 of the 27 silent misroutes: guardian only knew PROHIBITIONS
    def test_must_always_is_a_rule(self):
        assert subdomain_for("you must always verify a backup before deleting the source", 0) == 4

    def test_should_always_is_a_rule(self):
        assert subdomain_for("you should always test the backup power", 0) == 4

    def test_prohibition_rules_still_fire(self):
        assert subdomain_for("you must never share the vault contents", 0) == 4

    def test_plain_second_person_still_omega(self):
        assert subdomain_for("you handled the reflection well", 0) == 6


class TestAlignmentPriorityPhrases:
    # 12 of 27: 'choice' (weak decision token) outranked 'comes first'
    def test_comes_first_beats_choice_token(self):
        assert subdomain_for("soil health comes first in every planting choice", 3) == 4

    def test_matters_more_survives_decision_words(self):
        assert subdomain_for("flavor matters more than yield in what we pick", 3) == 4

    def test_bare_choice_still_decisions(self):
        assert subdomain_for("the choice was clay pots for the herb wall", 3) == 1


class TestNeverMindIdiom:
    # all 6 negation false-negatives were one idiom
    def test_never_mind_does_not_negate(self):
        assert subdomain_for("the probe tool works, never mind the noise", 5) == 0

    def test_real_following_never_still_widens(self):
        assert subdomain_for("the session never actually started", 9) is None


class TestWhitespaceNormalization:
    # multi-space broke literal-space phrase regexes
    def test_double_space_in_phrase(self):
        assert subdomain_for("flavor  matters  more  than shelf life here !!", 3) == 4

    def test_double_space_rule_of_thumb(self):
        assert subdomain_for("our  rule  of  thumb is taste before harvest !!", 3) == 3


class TestNegationClauseBoundary:
    # 'did not take AND failed' — negation must not cross the conjunction
    def test_negation_stops_at_conjunction(self):
        assert subdomain_for("the old pear graft did not take and failed", 4) == 6

    def test_negation_within_clause_still_suppresses(self):
        assert subdomain_for("the graft did not fail", 4) is None


class TestParaphraseLexicon:
    # keyword-free paraphrases that widened (each addition a general class)
    def test_went_out_is_delivery_milestone(self):
        assert subdomain_for("the cider press order went out monday", 9) == 2

    def test_nerves_noun_is_sentiment(self):
        assert subdomain_for("there were real nerves ahead of the inspection", 4) == 7

    def test_ended_in_failure_is_outcome(self):
        assert subdomain_for("grafting the old pear ended in failure", 4) == 6

    def test_limited_to_is_constraint(self):
        assert subdomain_for("we are limited to a two-acre water budget", 3) == 2

    def test_pending_is_progress(self):
        assert subdomain_for("the irrigation controller patch is still pending", 11) == 1

    def test_came_out_of_build_is_artifact(self):
        assert subdomain_for("a calibration file came out of the sensor build", 11) == 0


class TestRecallLobeMisses:
    # 12/60 fresh questions fell to the L2 default or got hijacked
    def test_defining_characteristics_is_identity(self):
        assert _lobe("what are my defining characteristics") == 0

    def test_hard_rules_question_is_identity(self):
        assert _lobe("what are the hard rules about backups") == 0

    def test_when_did_question_is_temporal(self):
        assert _lobe("when did we deliver the first honey batch") == 9

    def test_when_do_i_question_is_temporal(self):
        assert _lobe("when do i answer messages fastest") == 9

    def test_which_pipeline_is_procedure(self):
        assert _lobe("which pipeline runs the seed catalog build") == 5

    def test_bundle_from_build_is_dev(self):
        assert _lobe("where is the almanac bundle from the nightly build") == 11

    def test_how_do_we_verb_is_procedure(self):
        assert _lobe("how do we patch the irrigation controller firmware") == 5

    def test_repo_question_is_dev(self):
        assert _lobe("what do you remember about the implementation in the pump repo") == 11

    def test_who_action_verb_is_situational(self):
        assert _lobe("who left the greenhouse door open") == 4

    def test_why_fail_last_night_is_temporal_or_situational(self):
        assert _lobe("why did the irrigation pump fail last night") in (4, 5, 9)

    # counter-direction: the untouched defaults must hold
    def test_knowledge_query_stays_knowledge(self):
        assert _lobe("what do you know about beekeeping seasons") == 2

    def test_person_query_stays_persona(self):
        assert _lobe("who is Marcus") == 1
