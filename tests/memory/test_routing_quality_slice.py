"""Routing quality slice (2026-07-04) — RED cases lifted verbatim from the
stress battery's failing rows (scripts/memory_stress_report.py, corpus
memory/routing/stress_corpus.py). Targets: findability 94.1%→98%+, negation
over-trigger 10.7%→0, silent misroutes 2→0 — WITHOUT regressing the angles
already at 100% (paraphrase, noise, term-match), which the battery re-run pins.

Every fix asserts BOTH directions per the RED doctrine: the failing case now
widens/routes correctly AND the neighbouring correct behavior still fires.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from auto_loop.mril_tools.subdomain_helpers import subdomain_for


# ── Fix A: negation / hypothetical holes (expected: widen = None) ────────────


class TestNegationHoles:
    def test_noun_trigger_with_following_never_widens(self):
        # stress: L9 → sessions :: 'session' present, negated by following verb
        assert subdomain_for("the session never actually started", 9) is None

    def test_first_person_would_never_is_hypothetical(self):
        # stress: L0 → guardian :: bare-'never' fired on a disposition statement
        assert subdomain_for("I would never call privacy non-negotiable lightly", 0) is None

    def test_its_not_like_idiom_widens(self):
        # stress: L3 → goals :: 'not' sat outside the 5-word look-back window
        assert subdomain_for("it's not like we have a clear goal here", 3) is None

    # The gate's other direction — the guard must NOT over-suppress:
    def test_plain_session_still_routes(self):
        assert subdomain_for("the session started on time", 9) == 0  # sessions

    def test_imperative_never_still_makes_a_rule(self):
        # 'you must never X' is a guardian rule, not a hypothetical
        assert subdomain_for("you must never share the vault contents", 0) == 4

    def test_negation_after_window_does_not_suppress_verbs(self):
        # following negation beyond the noun look-ahead must not suppress
        assert subdomain_for("the goal is important, not optional", 3) == 0  # goals


# ── Fix B: write↔recall phrasing symmetry (one resolver, both phrasings) ─────


class TestSymmetry:
    def test_operator_possessive_kin_recall_matches_write(self):
        # stress symmetry miss: write 'she is my sister' → (0,1); recall
        # "who is the operator's sister" fell to the operator fallback → (0,0)
        write_y = subdomain_for("she is my sister", 0)
        recall_y = subdomain_for("who is the operator's sister", 0)
        assert write_y == recall_y == 1

    def test_operator_as_subject_still_constitution(self):
        # non-possessive operator-as-subject keeps the constitution fallback
        assert subdomain_for("the operator prefers local-first designs", 0) == 0


# ── Fix D: the two silent write misroutes (the dangerous 1.6%) ───────────────


class TestSilentMisroutes:
    def test_self_model_evolution_routes_to_evolution(self):
        # stress: L0 exp=evolution got=constitution ('my' captured it)
        assert subdomain_for("my self-model has evolved over months", 0) == 3

    def test_system_evolution_no_pronoun_also_routes(self):
        # was an unsorted_miss — evolution had NO grammar trigger at all
        assert subdomain_for("the system evolved new caution after the rollback", 0) == 3

    def test_leading_goal_beats_embedded_decide(self):
        # stress: L3 exp=goals got=decisions ('decide' outranked leading 'goal')
        assert subdomain_for("the goal is to decide which subdomains to add", 3) == 0

    def test_decision_without_goal_token_still_decisions(self):
        assert subdomain_for("we decided to keep it local", 3) == 1

    def test_decide_before_goal_stays_decisions(self):
        # positional tie-break only flips when the goal token leads
        assert subdomain_for("we decided the goal for next week", 3) == 1


# ── Fix C: _unsorted recall widening at the remember_* executor ──────────────


def _fake_verdict(candidates: List[Dict[str, Any]]):
    class _V:
        status = "verified" if candidates else "empty"
        reason = "fake"
        answer_payload = {"candidates": candidates}

    return _V()


class TestUnsortedRecallWiden:
    def _run(self, monkeypatch, first_hits, widened_hits):
        """Drive run_remember_tool with an injected read_fn that returns
        ``first_hits`` for the grammar-scoped query and ``widened_hits`` once
        UNSORTED_SUBDOMAIN_Y is inside allowed_subdomains."""
        from auto_loop.mril_tools import remember as rem
        from memory.dewey_map import UNSORTED_SUBDOMAIN_Y

        calls: List[List[int]] = []

        def read_fn(spec):
            subs = list(spec.allowed_subdomains or [])
            calls.append(subs)
            if UNSORTED_SUBDOMAIN_Y in subs:
                return _fake_verdict(widened_hits)
            return _fake_verdict(first_hits)

        out = rem.invoke_remember_tool(
            {
                "tool": "remember_time",
                "query": "what happened in the deploy session",
                "allowed_lobes": [9],
                "memory_type": "temporal",
                "lane": "temporal",
                "lobe": 9,
                "subdomain": 0,  # grammar-scoped → sessions
            },
            "ep-test",
            memory=read_fn,
        )
        return out, calls

    def test_empty_scoped_recall_widens_into_unsorted(self, monkeypatch):
        from memory.dewey_map import UNSORTED_SUBDOMAIN_Y

        hit = {
            "coord": [9, UNSORTED_SUBDOMAIN_Y, 0],
            "title": "deploy session note",
            "summary": "the deploy session stalled",
            "confidence": 0.9,
        }
        out, calls = self._run(monkeypatch, first_hits=[], widened_hits=[hit])
        # the retry happened and included _unsorted
        assert len(calls) == 2
        assert UNSORTED_SUBDOMAIN_Y in calls[1]
        # the widened hit reached the envelope
        assert any("deploy session" in str(e) for e in out.get("evidence", []))

    def test_scoped_hit_never_triggers_widen(self, monkeypatch):
        hit = {
            "coord": [9, 0, 0],
            "title": "session log",
            "summary": "the deploy session log",
            "confidence": 0.9,
        }
        out, calls = self._run(monkeypatch, first_hits=[hit], widened_hits=[])
        assert len(calls) == 1  # no second query — scoping stays authoritative

    def test_explicit_allowed_subdomains_contract_untouched(self, monkeypatch):
        # a caller-provided allowed_subdomains list is an explicit contract —
        # the widen applies ONLY to grammar-scoped single-Y entries
        from auto_loop.mril_tools import remember as rem

        calls: List[List[int]] = []

        def read_fn(spec):
            calls.append(list(spec.allowed_subdomains or []))
            return _fake_verdict([])

        rem.invoke_remember_tool(
            {
                "tool": "remember_time",
                "query": "what happened",
                "allowed_lobes": [9],
                "memory_type": "temporal",
                "lane": "temporal",
                "lobe": 9,
                "allowed_subdomains": [0, 1],
            },
            "ep-test",
            memory=read_fn,
        )
        assert len(calls) == 1


# ── Slice 2 (2026-07-04): "make a statement" — kill the remaining defer class,
# the recall-lobe miss, and the L2-default gravity on ambiguous queries. Every
# rule is GENERAL (tradeoff=decision, priority=alignment, release=milestone,
# stance-verb=constitution, proper-name=persona, grammar-probe=lobe choice) —
# the battery's untouched angles pin against corpus-overfitting. ──


class TestDeferClassKilled:
    """The 12 grammar-blind writes that fell to _unsorted, by general rule."""

    def test_stance_verb_is_constitution(self):
        assert subdomain_for("Noah believes the device should outlive its vendor", 0) == 0

    def test_named_person_is_person_stub(self):
        assert subdomain_for("Sarah runs the lab next door", 1) == 1
        assert subdomain_for("Dr. Lee teaches the evening seminar", 1) == 1

    def test_tradeoff_language_is_decision(self):
        assert subdomain_for("we weighed latency against privacy and kept it local", 3) == 1

    def test_priority_statement_is_alignment(self):
        assert subdomain_for("keeping routing deterministic matters more than breadth", 3) == 4

    def test_emotion_noun_is_sentiment(self):
        assert subdomain_for("there was real anxiety before the launch", 4) == 7

    def test_success_failure_events_are_outcome(self):
        assert subdomain_for("the build finally succeeded after the gate fix", 4) == 6
        assert subdomain_for("the deploy failed at 3am and we rolled back", 4) == 6

    def test_tool_component_vocab_is_cognitive(self):
        assert subdomain_for("the helper that wraps the model endpoint", 5) == 0

    def test_screen_is_visual(self):
        assert subdomain_for("what is on the screen right now", 7) == 0

    def test_release_language_is_milestone(self):
        assert subdomain_for("we shipped the first beta on Tuesday", 9) == 2

    def test_still_need_is_progress(self):
        assert subdomain_for("we still need to wire the loader", 11) == 1


class TestRecallLobeAccuracy:
    def test_codebase_question_with_intervening_words_reaches_dev(self):
        from auto_loop.mril_tools.router import route_memory_tool

        e = route_memory_tool("what do you remember about the implementation in this codebase")
        assert e["lobe"] == 11 and e["tool"] == "remember_dev"


class TestGrammarProbeDisambiguation:
    """Ambiguous queries land in a lobe whose grammar RECOGNIZES the text
    instead of defaulting to knowledge(2)."""

    def _lobe(self, q):
        from auto_loop.mril_tools.router import route_memory_tool

        return route_memory_tool(q)["lobe"]

    def test_session_phrase_lands_temporal(self):
        assert self._lobe("the session where we decided the schema") == 9

    def test_rule_phrase_lands_guardian_identity(self):
        assert self._lobe("you should never deploy without tests") == 0

    def test_artifact_phrase_lands_dev(self):
        assert self._lobe("the artifact from the build broke in testing") == 11

    def test_face_phrase_lands_vision(self):
        assert self._lobe("the scene from the camera showed a new face") == 7

    def test_constraint_phrase_lands_reasoning(self):
        assert self._lobe("the constraint forced us to refactor the cache") == 3

    # counter-direction: the probe must NOT hijack genuine knowledge queries
    def test_generic_knowledge_query_stays_knowledge(self):
        assert self._lobe("what do you know about quantum entanglement") == 2

    def test_want_to_understand_stays_knowledge(self):
        # 'want' is a goal token but too generic to claim the reasoning lobe
        assert self._lobe("I want to understand how neurons encode memory") == 2

    def test_how_to_still_procedure(self):
        assert self._lobe("how to write async python correctly") == 5


class TestFinalSymmetryMiss:
    def test_build_production_statement_is_artifact(self):
        # the last findability miss: write had no artifact noun, only the
        # production verb — which IS the artifact signal (mirrors the router's
        # 'what did the build produce' recall intent)
        assert subdomain_for("the build produced a signed APK", 11) == 0
        assert subdomain_for("where is the build artifact", 11) == 0
