"""Dependency-parse fact extraction — one grammar rule per fact, every phrasing.

Proves the parse-based extractor lifts structured facets out of arbitrary English
where the regex enricher had a per-phrasing pattern (and missed ownership entirely).
Skips cleanly if spaCy/en_core_web_sm isn't installed so CI without the model is
green rather than red.
"""

from __future__ import annotations

import pytest

from memory.enrichment.parse_extract import extract_facts

pytestmark = pytest.mark.skipif(
    extract_facts("I am Noah.") == [] and __import__("importlib").util.find_spec("spacy") is None,
    reason="spaCy / en_core_web_sm not installed",
)


def _facets(facts, facet):
    return [f for f in facts if f.get("facet") == facet]


def _values(facts, facet):
    return [str(f.get("name", "")).lower() for f in _facets(facts, facet)]


def test_spacy_load_failure_reaches_the_extractor_caller(monkeypatch):
    import memory.enrichment.parse_extract as parser
    import spacy

    monkeypatch.setattr(parser, "_NLP", None)
    monkeypatch.setattr(
        spacy,
        "load",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("spaCy model unavailable")),
    )

    with pytest.raises(RuntimeError, match="spaCy model unavailable"):
        parser.extract_facts("I work at Omega AI.")


def test_persona_enrichment_propagates_parser_failure(monkeypatch):
    import memory.enrichment.parse_extract as parser
    from memory.enrichment.personas import enrich

    monkeypatch.setattr(
        parser,
        "extract_facts",
        lambda _text: (_ for _ in ()).throw(RuntimeError("parser execution failed")),
    )

    with pytest.raises(RuntimeError, match="parser execution failed"):
        enrich("I work at Omega AI.")


# ── The operator's exact live statement — the whole point ─────────────────────
def test_live_multiclause_statement_yields_every_fact():
    facts = extract_facts(
        "My name is Noah. I have a son named Abel, and I own a business called Omega AI."
    )
    assert "noah" in _values(facts, "name")
    rel = _facets(facts, "relationship")
    assert any(f["name"] == "son" and f["linked_name"].lower() == "abel" for f in rel), rel
    assert "omega ai" in _values(facts, "business"), facts


# ── Business ownership — the case regex dropped, across phrasings ─────────────
@pytest.mark.parametrize(
    "text",
    [
        "I own a business called Omega AI.",
        "I run a company called Omega AI.",
        "I founded Omega AI.",
        "I started a startup called Omega AI.",
        "me and my brother started Omega AI in the garage.",
    ],
)
def test_business_ownership_various_phrasings(text):
    assert "omega ai" in _values(extract_facts(text), "business"), text


def test_owning_a_house_is_not_a_business():
    # "own" without a business object must NOT mint a business fact.
    assert _facets(extract_facts("I own a house in Denver."), "business") == []


# ── Relationships — named / apposition / conjunction ─────────────────────────
def test_relationship_named():
    rel = _facets(extract_facts("I have a son named Abel."), "relationship")
    assert any(f["name"] == "son" and f["linked_name"].lower() == "abel" for f in rel)


def test_relationship_apposition():
    rel = _facets(extract_facts("My sister Miranda is a nurse."), "relationship")
    assert any(f["name"] == "sister" and f["linked_name"].lower() == "miranda" for f in rel)


@pytest.mark.parametrize(
    "phrase,relation,person",
    [
        ("My mother's name is Kim.", "mother", "kim"),
        ("My mothers name is Kim.", "mother", "kim"),  # no apostrophe (operator's live form)
        ("My mom's name is Kim.", "mom", "kim"),
        ("My dad's name is Bob.", "dad", "bob"),
        ("My uncle's name is Sam.", "uncle", "sam"),
        ("My sister's name is Miranda.", "sister", "miranda"),
        ("My brother's name is Abel.", "brother", "abel"),
        ("My cousin's name is Lee.", "cousin", "lee"),
        ("My grandma's name is Rose.", "grandma", "rose"),
    ],
)
def test_relation_possessive_name_is_a_relationship(phrase, relation, person):
    # "My <relation>'s name is X" — the name belongs to the RELATION, not the operator.
    # (Live bug: "my mothers name is Kim" minted "operator's name is kim".)
    rel = {
        (f["name"], str(f.get("linked_name", "")).lower())
        for f in extract_facts(phrase)
        if f.get("facet") == "relationship"
    }
    assert (relation, person) in rel, phrase


def test_subject_attribution_operator_vs_third_person():
    # operator self-facts carry subject "operator".
    ops = extract_facts("I own a company called Omega AI. I have a son named Abel.")
    biz = next(f for f in ops if f["facet"] == "business")
    son = next(f for f in ops if f["facet"] == "relationship")
    assert biz["subject"] == "operator"
    assert son["subject"] == "operator"

    # a THIRD-PERSON fact hangs off the named subject, not the operator.
    other = extract_facts("Abel likes trains.")
    pref = next(f for f in other if f["facet"] == "preference")
    assert pref["subject"] == "abel"
    assert pref["name"] == "trains"


def test_plain_my_name_stays_the_operator_name():
    facts = extract_facts("My name is Noah.")
    assert "noah" in [str(f["name"]).lower() for f in facts if f.get("facet") == "name"]
    assert not any(f.get("facet") == "relationship" for f in facts)


def test_relationship_conjunction_splits_into_two():
    rel = _facets(
        extract_facts("I have a sister named Miranda and a son named Abel."),
        "relationship",
    )
    pairs = {(f["name"], f["linked_name"].lower()) for f in rel}
    assert ("sister", "miranda") in pairs and ("son", "abel") in pairs, pairs


# ── Other self-facets, generically ───────────────────────────────────────────
def test_name_two_phrasings():
    assert "noah" in _values(extract_facts("I'm Noah."), "name")
    assert "noah" in _values(extract_facts("My name is Noah."), "name")


def test_occupation_from_copula():
    assert "cybersecurity student" in _values(
        extract_facts("I am a cybersecurity student."), "occupation"
    )


def test_workplace_and_location():
    assert "google" in _values(extract_facts("I work at Google."), "workplace")
    assert "denver" in _values(extract_facts("I live in Denver."), "location")


def test_education_field_of_study():
    assert "cybersecurity" in _values(
        extract_facts("I go to college for cybersecurity."), "education"
    )


@pytest.mark.parametrize(
    "text",
    [
        # spaCy flattens these as sibling PPs on the verb ("going [to college]
        # [for Bachelors] [in cybersecurity]") — the field hangs off the VERB, not the
        # academic anchor. The live miss: "where do I go to school?" recalled nothing.
        "I am going to college for my Bachelors in cybersecurity.",
        "I'm getting my degree in cybersecurity.",
        "I'm pursuing a Bachelors in cybersecurity at college.",
    ],
)
def test_education_field_via_sibling_pp(text):
    assert "cybersecurity" in _values(extract_facts(text), "education"), text


def test_education_still_ignores_mundane_for_phrase():
    # "for milk" must not become a field of study: no academic anchor present.
    assert _facets(extract_facts("I go to the store for milk."), "education") == []


# ── Business: not just ownership verbs (live miss: "I have a business called X") ──
@pytest.mark.parametrize(
    "text",
    [
        "I own a business called Omega AI.",
        "I have a business called Omega AI.",
        "I have a company called Omega AI.",
        "Omega AI is my business.",
        "Omega AI is my company.",
    ],
)
def test_business_various_verbs_and_copula(text):
    assert "omega ai" in _values(extract_facts(text), "business"), text


def test_have_a_relation_is_not_a_business():
    # "have" must only mint a business for a BUSINESS-noun object, never "a son".
    assert _facets(extract_facts("I have a son named Abel."), "business") == []


# ── Education: a place (GPE) is never a field of study (live "phoenix" misparse) ──
def test_education_location_is_not_the_field():
    # "school in Phoenix" — Phoenix is a city, not a field. Must NOT become education.
    assert _values(extract_facts("I go to school in Phoenix."), "education") == []


def test_education_real_field_wins_over_location():
    assert "cybersecurity" in _values(
        extract_facts("I go to school in Phoenix for cybersecurity."), "education"
    )


# ── Determinism (the doctrine requirement) ───────────────────────────────────
def test_extraction_is_deterministic():
    text = "I own a business called Omega AI and I have a son named Abel."
    runs = [extract_facts(text) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
