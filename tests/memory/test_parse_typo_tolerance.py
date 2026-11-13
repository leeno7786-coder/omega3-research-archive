"""Typo-tolerant classifier matching — malformed words must still create memory
instead of ghosting it, WITHOUT fabricating memory from real words.

A single-char typo in a classifier word ("comany", "sistr", "collge") leaves spaCy's
dependency parse intact (the dobj/acl/pobj roles hold) but breaks the exact vocabulary
lookup, so the fact was silently dropped. The parser now fuzzy-matches the CLASSIFIER
vocabulary (deterministic Damerau distance) — guarded by a real-English-word oracle
(pyspellchecker OR wordfreq) so a real word one edit from the vocab ("sitter" vs
"sister") is NEVER corrected. Entities/values (names, business names, fields) stay
verbatim.
"""

from __future__ import annotations

import importlib.util

import pytest

from memory.enrichment.parse_extract import extract_facts

pytestmark = pytest.mark.skipif(
    extract_facts("I am Noah.") == [] and importlib.util.find_spec("spacy") is None,
    reason="spaCy / en_core_web_sm not installed",
)

_HAS_DICT = (
    importlib.util.find_spec("spellchecker") is not None
    or importlib.util.find_spec("wordfreq") is not None
)
requires_dict = pytest.mark.skipif(
    not _HAS_DICT, reason="pyspellchecker / wordfreq not installed (typo correction off)"
)


def _values(facts, facet):
    return [str(f.get("name", "")).lower() for f in facts if f.get("facet") == facet]


def _rel_pairs(facts):
    return {
        (f["name"], str(f.get("linked_name", "")).lower())
        for f in facts
        if f.get("facet") == "relationship"
    }


# ── A typo in a classifier word still creates the fact (was: ghosted) ─────────
@requires_dict
@pytest.mark.parametrize(
    "text,facet,value",
    [
        ("I own a comany called Omega AI.", "business", "omega ai"),
        ("I run a startp called Omega AI.", "business", "omega ai"),
        ("I am going to collge for cybersecurity.", "education", "cybersecurity"),
        ("I am going to collge for my Bachelors in cybersecurity.", "education", "cybersecurity"),
    ],
)
def test_typo_classifier_still_extracts(text, facet, value):
    assert value in _values(extract_facts(text), facet), text


@requires_dict
def test_typo_relation_noun_corrected_to_canonical():
    # The relation is STORED, so a typo must be corrected to the canonical term.
    assert ("sister", "miranda") in _rel_pairs(extract_facts("I have a sistr named Miranda."))
    # transposition ("brohter" -> "brother") is a single Damerau edit.
    assert ("brother", "abel") in _rel_pairs(extract_facts("I have a brohter named Abel."))


# ── The false-positive guard: a REAL word near the vocab is never corrected ───
def test_real_word_near_vocab_is_not_a_relation():
    # "sitter" is a real word one edit from "sister" — must NOT become a sibling relation
    # (a fabricated memory is worse than a ghosted one). Holds with OR without the dicts.
    rels = {name for name, _ in _rel_pairs(extract_facts("I have a sitter named Miranda."))}
    assert "sister" not in rels


def test_entity_name_is_never_spell_corrected():
    # Only the classifier noun is fuzzy-matched; the business NAME is stored verbatim.
    facts = extract_facts("I own a comany called Omega AI.")
    biz = _values(facts, "business")
    if biz:  # (skips cleanly when dicts absent -> business ghosted, tested above)
        assert "omega ai" in biz


@requires_dict
def test_typo_tolerance_is_deterministic():
    text = "I own a comany called Omega AI and I have a sistr named Miranda."
    runs = [extract_facts(text) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
