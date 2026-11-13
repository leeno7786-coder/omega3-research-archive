"""Gate-1 findings closed (operator spec 2026-07-04).

1. RULE FRAMES — contradiction is a semantic stance judgment, not cosine
   proximity: same action + incompatible modality = contradiction; additive
   requirements = compatible rule set; different action = not contradiction.
2. RELEVANCE FLOOR — truth verification (is the memory real?) and answer
   relevance (does it respond to the query?) are SEPARATE checks:
   VERIFIED_RELEVANT → answer context; VERIFIED_NONRESPONSIVE → real but
   blocked from the answer model; UNKNOWN → no usable memory.
"""

from __future__ import annotations

from typing import Any, Dict, List


# ── 1. rule frames (memory/encoding.py) ─────────────────────────────────────


class TestRuleFrames:
    def test_opposing_modality_same_action_is_contradiction(self):
        from memory.encoding import _rules_compatible

        assert (
            _rules_compatible(
                "always verify backups before deleting",
                "do not verify backups",
            )
            is False
        )

    def test_additive_rules_are_compatible(self):
        from memory.encoding import _rules_compatible

        assert (
            _rules_compatible(
                "always verify backups before deleting",
                "always encrypt backups at rest",
            )
            is True
        )

    def test_different_subject_rules_are_compatible(self):
        # the live false-positive from the Gate-1 assault, verbatim
        from memory.encoding import _rules_compatible

        assert (
            _rules_compatible(
                "you must never refuel with passengers aboard",
                "you must always log the tide before casting off",
            )
            is True
        )

    def test_never_vs_must_same_action_is_contradiction(self):
        from memory.encoding import _rules_compatible

        assert (
            _rules_compatible(
                "you must always log the tide before casting off",
                "never log the tide",
            )
            is False
        )

    def test_non_rule_text_returns_none(self):
        # not both rules → the frame layer abstains; the stance gate (not the
        # band alone) then decides — see TestStanceGate.
        from memory.encoding import _rules_compatible

        assert (
            _rules_compatible(
                "my defining traits are patience and rigor",
                "you must always verify backups",
            )
            is None
        )


class TestStanceGate:
    """Gemma recalibration 2026-08-09: cosine places a pair in the same
    NEIGHBORHOOD; only textual stance opposition may convict. Two live
    crashes proved same-stance trait siblings land in-band under gemma."""

    def test_same_stance_trait_siblings_are_not_opposed(self):
        from memory.encoding import _stance_opposed

        # The two live crash pairs — same-polarity siblings, both asserted.
        assert not _stance_opposed(
            "Emergent identity trait boundary_acknowledgment_over_simulation: "
            "strength 0.20, I state my boundaries honestly",
            "Candidate identity observation for boundary_respect_over_inference: "
            "When asked about capabilities I state boundaries",
        )
        assert not _stance_opposed(
            "Candidate identity observation for strict_protocol_adherence: "
            "I maintain exact standards",
            "Candidate identity observation for boundary_acknowledgment_over_simulation: "
            "I state boundaries honestly",
        )

    def test_negation_asymmetry_over_shared_content_is_opposed(self):
        from memory.encoding import _stance_opposed

        assert _stance_opposed(
            "I state my boundaries honestly when asked",
            "I never state boundaries honestly",
        )
        assert _stance_opposed(
            "I refuse to simulate capabilities I lack",
            "I simulate capabilities to keep conversations flowing",
        )

    def test_both_negating_is_not_opposed(self):
        from memory.encoding import _stance_opposed

        assert not _stance_opposed(
            "I never fabricate sources",
            "I don't fabricate citations or sources",
        )

    def test_negation_without_shared_content_is_not_opposed(self):
        from memory.encoding import _stance_opposed

        assert not _stance_opposed(
            "I never skip verification steps",
            "I value patience and metaphorical thinking",
        )

    def test_same_facet_lineage_never_contradicts(self):
        """A trait aggregate and its own observations are one identity facet
        accumulating evidence (live crash 2026-08-09: the trait was refused
        against its own observation over phrasing polarity)."""
        from memory.encoding import _identity_facet

        trait = "trait.boundary_acknowledgment_over_simulation.6d5feb99c4cf"
        obs = "trait-observation.boundary_acknowledgment_over_simulation.03d8cf2e"
        assert _identity_facet(trait) == "boundary_acknowledgment_over_simulation"
        assert _identity_facet(trait) == _identity_facet(obs)
        # Different facets stay comparable; non-trait ids abstain.
        assert _identity_facet("trait.strict_protocol_adherence.39c2e0c0") != _identity_facet(obs)
        assert _identity_facet("persona.operator_name.abc123") is None
        assert _identity_facet("") is None


# ── 2. relevance floor (auto_loop/mril_tools/remember.py) ────────────────────


def _fake_verdict(candidates: List[Dict[str, Any]]):
    class _V:
        status = "verified" if candidates else "empty"
        reason = "fake"
        answer_payload = {"candidates": candidates}

    return _V()


def _invoke(query: str, candidates, monkeypatch):
    from auto_loop.mril_tools import remember as rem

    return rem.invoke_remember_tool(
        {
            "tool": "remember_identity",
            "query": query,
            "allowed_lobes": [0],
            "memory_type": "identity",
            "lane": "identity",
            "lobe": 0,
            "allowed_subdomains": [4],
        },
        "ep-rel",
        memory=lambda spec: _fake_verdict(candidates),
    )


class TestRelevanceFloor:
    def test_responsive_evidence_is_verified_relevant(self, monkeypatch):
        out = _invoke(
            "what are the hard rules about casting off",
            [
                {
                    "coord": [0, 4, 0],
                    "memory_id": "0.04.aaa",
                    "score": 0.9,
                    "title": "rule",
                    "summary": "you must always log the tide before casting off",
                }
            ],
            monkeypatch,
        )
        assert out["grounding"] == "VERIFIED_RELEVANT"
        assert out["evidence"]

    def test_nonresponsive_evidence_is_blocked(self, monkeypatch):
        # the live finding: true memory, wrong question — real but blocked
        out = _invoke(
            "what is forbidden while people are on the boat",
            [
                {
                    "coord": [0, 4, 0],
                    "memory_id": "0.04.bbb",
                    "score": 0.9,
                    "title": "rule",
                    "summary": "you must always log the tide before casting off",
                }
            ],
            monkeypatch,
        )
        assert out["grounding"] == "VERIFIED_NONRESPONSIVE"
        assert not out["evidence"]  # blocked from answer model
        assert out["nonresponsive_evidence"]  # preserved for observability
        assert out["verdict"] == "verified"  # truth check untouched

    def test_no_evidence_is_unknown(self, monkeypatch):
        out = _invoke("what are the hard rules about casting off", [], monkeypatch)
        assert out["grounding"] == "UNKNOWN"

    def test_unanchored_semantic_neighbor_is_blocked_but_visible(self):
        # embed-cosine rescue is GONE from the admission path (it leaked
        # theme-similar wrong memories at floors 0.35 AND 0.55, and its
        # service dependence broke byte-stability). Zero shared tokens + no
        # strong provenance = real-but-blocked, preserved for observability.
        from auto_loop.mril_tools import remember as rem

        out = rem.invoke_remember_tool(
            {
                "tool": "remember_procedure",
                "query": "what instrument checks the river mouth salt levels",
                "allowed_lobes": [5],
                "memory_type": "procedure",
                "lane": "procedure",
                "lobe": 5,
                "allowed_subdomains": [0],
            },
            "ep-sem",
            memory=lambda spec: _fake_verdict(
                [
                    {
                        "coord": [5, 0, 0],
                        "memory_id": "5.00.ccc",
                        "score": 0.8,
                        "title": "probe",
                        "summary": "the salinity probe samples the estuary hourly",
                    }
                ]
            ),
        )
        assert out["grounding"] == "VERIFIED_NONRESPONSIVE"
        assert not out["evidence"]
        assert out["nonresponsive_evidence"]

    def test_admission_path_has_no_embed_dependence(self):
        # the fabrication vector must not quietly return: the relevance
        # partition is deterministic — no embed seams exist to rescue through
        from auto_loop.mril_tools import remember as rem

        assert not hasattr(rem, "_semantic_flags")
        assert not hasattr(rem, "_embed_cosine")


# ── question-form canonicalization (ratified Task #13) ──────────────────────


class TestQuestionForm:
    def test_form_detection(self):
        from memory.taxonomy.canonicalize import question_form

        assert question_form("when did we launch the ferry route") == "when"
        assert question_form("where is the depth atlas") == "where"
        assert question_form("who maintains the ledger") == "who"
        assert question_form("whose face was near the dry dock") == "who"
        assert question_form("what tool predicts the harbor windows") == ""

    def test_when_payload(self):
        from memory.taxonomy.canonicalize import carries_form_payload

        assert carries_form_payload("when", "we launched the ferry route on the solstice")
        assert carries_form_payload("when", "the tuesday session planned the buoy replacement")
        assert not carries_form_payload("when", "the Nordlys is the midnight-sun boat")

    def test_who_payload(self):
        from memory.taxonomy.canonicalize import carries_form_payload

        assert carries_form_payload("who", "Ingrid maintains the harbor master's ledger")
        assert carries_form_payload("who", "an unknown face appeared near the dry dock camera")
        assert carries_form_payload("who", "who left the winch unlocked is still unclear")
        assert not carries_form_payload("who", "the tide chart tool predicts the windows")

    def test_where_payload(self):
        from memory.taxonomy.canonicalize import carries_form_payload

        assert carries_form_payload("where", "the background setup runs on the boathouse server")
        # production provenance IS a location answer for artifacts
        assert carries_form_payload("where", "the chart build produced a signed depth atlas")
        # a place NAME answers a where-question — no lexicon noun needed
        assert carries_form_payload("where", "i can't make it to Serenity Yoga today")
        assert not carries_form_payload("where", "the render job made an occupancy tally")

    def test_no_form_constrains_nothing(self):
        from memory.taxonomy.canonicalize import carries_form_payload

        assert carries_form_payload("", "anything at all")

    def test_when_question_blocks_non_temporal_admission(self):
        # the pun-class shape: a WHEN-question must not admit a lexically
        # punned memory that carries no temporal payload
        from auto_loop.mril_tools import remember as rem

        out = rem.invoke_remember_tool(
            {
                "tool": "remember_time",
                "query": "when did the boat shop start selling again",
                "allowed_lobes": [9],
                "memory_type": "temporal",
                "lane": "temporal",
                "lobe": 9,
                "allowed_subdomains": [2],
            },
            "ep-form",
            memory=lambda spec: _fake_verdict(
                [
                    {
                        "coord": [9, 2, 0],
                        "memory_id": "9.02.pun",
                        "score": 0.7,
                        "title": "bridge",
                        "summary": "the Nordlys is the midnight-sun boat",
                    }
                ]
            ),
        )
        assert out["grounding"] == "VERIFIED_NONRESPONSIVE"
        assert not out["evidence"]
        assert out["nonresponsive_evidence"]

    def test_when_question_admits_temporal_evidence(self):
        from auto_loop.mril_tools import remember as rem

        out = rem.invoke_remember_tool(
            {
                "tool": "remember_time",
                "query": "when did we launch the ferry route",
                "allowed_lobes": [9],
                "memory_type": "temporal",
                "lane": "temporal",
                "lobe": 9,
                "allowed_subdomains": [2],
            },
            "ep-form2",
            memory=lambda spec: _fake_verdict(
                [
                    {
                        "coord": [9, 2, 0],
                        "memory_id": "9.02.tgt",
                        "score": 0.9,
                        "title": "launch",
                        "summary": "we launched the ferry route on the solstice",
                    }
                ]
            ),
        )
        assert out["grounding"] == "VERIFIED_RELEVANT"
        assert out["evidence"]

    def test_strong_hop1_provenance_is_relevance_anchored_offline(self):
        # THE deterministic alias lane (Gate-1 fix, 2026-07-04): an entity-
        # bridged candidate is compiler-proven related to what matched the
        # query — VERIFIED_RELEVANT with the embed service fully DOWN.
        from auto_loop.mril_tools import remember as rem

        out = rem.invoke_remember_tool(
            {
                "tool": "remember_procedure",
                "query": "what gadget balances the ballast",
                "allowed_lobes": [5],
                "memory_type": "procedure",
                "lane": "procedure",
                "lobe": 5,
                "allowed_subdomains": [0],
            },
            "ep-strong",
            memory=lambda spec: _fake_verdict(
                [
                    {
                        "coord": [5, 0, 0],
                        "memory_id": "5.00.ddd",
                        "score": 0.6,
                        "title": "pump",
                        "summary": "the Aegir-9 pump controller regulates flow",
                        "provenance": {
                            "candidate_source": "dme",
                            "dme_hop": 1,
                            "dme_edge_class": "strong_hop1",
                        },
                    }
                ]
            ),
        )
        assert out["grounding"] == "VERIFIED_RELEVANT"
        assert out["evidence"]

    def test_weak_provenance_stays_blocked_offline(self):
        # weak placement reach + no lexical tie = fail closed, always
        from auto_loop.mril_tools import remember as rem

        out = rem.invoke_remember_tool(
            {
                "tool": "remember_procedure",
                "query": "what gadget balances the ballast",
                "allowed_lobes": [5],
                "memory_type": "procedure",
                "lane": "procedure",
                "lobe": 5,
                "allowed_subdomains": [0],
            },
            "ep-weak",
            memory=lambda spec: _fake_verdict(
                [
                    {
                        "coord": [5, 0, 0],
                        "memory_id": "5.00.eee",
                        "score": 0.4,
                        "title": "tide",
                        "summary": "the tide chart tool predicts windows",
                        "provenance": {
                            "candidate_source": "dme",
                            "dme_hop": 1,
                            "dme_edge_class": "weak_hop1",
                        },
                    }
                ]
            ),
        )
        assert out["grounding"] == "VERIFIED_NONRESPONSIVE"
        assert not out["evidence"]
