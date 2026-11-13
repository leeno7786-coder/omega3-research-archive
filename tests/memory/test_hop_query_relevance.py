"""A hopped (via_link) neighbor must EARN its place by sharing a CONTENT token with
the query — not a stopword, not just a structural link. This weeds the cross-chain
junk (operator -> Abel on a "what is Python" query) while keeping relevant chains
(operator -> Abel on "tell me about Noah", since the name-bearing relation "Abel is
Noah Lee's son" contains "noah"). Phase 1a of the hop-scoring gate; lexical only,
semantic re-rank is Phase 2.
"""

from __future__ import annotations

from memory.taxonomy.recall import (
    _content_tokens,
    _hop_is_query_relevant,
    _hop_survives,
    _tokens,
)


def _meta(text, title="", tags=None, edge_type=""):
    return {
        "title": title,
        "summary": text,
        "text": text,
        "tags": tags or [],
        "_via_edge_type": edge_type,
    }


def test_content_tokens_drops_stopwords_and_question_words():
    content = _content_tokens(_tokens("what did you tell me about Noah"))
    assert "noah" in content
    for stop in ("what", "you", "tell", "me", "about", "did"):
        assert stop not in content, f"{stop!r} should be filtered as a stopword"


def test_relevant_hop_survives_when_it_shares_a_content_token():
    content = _content_tokens(_tokens("tell me about Noah"))
    abel = _meta("Abel is Noah Lee's son. The operator's son is Abel.")
    assert _hop_is_query_relevant(abel, content) is True


def test_irrelevant_hop_is_dropped():
    content = _content_tokens(_tokens("what is Python asyncio"))
    abel = _meta("Abel is Noah Lee's son. The operator's son is Abel.")
    assert _hop_is_query_relevant(abel, content) is False


def test_hop_matches_on_tags_and_title_too():
    content = _content_tokens(_tokens("who works on cybersecurity"))
    rec = _meta("some body text", title="Education", tags=["profile:cybersecurity"])
    assert _hop_is_query_relevant(rec, content) is True


def test_contentless_query_does_not_gate_hops():
    # An all-stopword query has no content tokens -> don't over-drop; keep hops.
    content = _content_tokens(_tokens("what is it"))
    assert content == set()
    assert _hop_is_query_relevant(_meta("anything at all"), content) is True


# ── _hop_survives: every via-link hop is structural and lexically floored ──
# RETIRED (Cut 5A Task 4): test_semantic_similarity_hop_is_exempt_from_the_lexical_floor
# asserted the SEMANTIC_SIMILARITY exemption ("the cosine is the signal"). The class
# and the exemption are gone; the positive proof of NO exemption is
# tests/memory/test_semantic_similarity_retired.py::test_hop_survives_has_no_semantic_exemption.


def test_structural_hop_without_query_term_is_dropped():
    content = _content_tokens(_tokens("what is Python asyncio"))
    abel = _meta("Abel is Noah Lee's son.", edge_type="MENTIONS")
    assert _hop_survives(abel, content) is False


def test_structural_hop_with_query_term_survives():
    content = _content_tokens(_tokens("tell me about Noah"))
    abel = _meta("Abel is Noah Lee's son.", edge_type="MENTIONS")
    assert _hop_survives(abel, content) is True
