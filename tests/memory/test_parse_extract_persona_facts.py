"""Persona-fact extraction defects measured live on 2026-07-31 (boot #1).

D1  "I attend the University of Phoenix where I'm taking my Bachelors in
    Science in Cybersecurity."  ->  education='phoenix'
    The institution head noun is discarded (only the pobj of "of" survives) and
    the degree is never extracted at any phrasing. Measured: the bare clause
    "I attend the University of Phoenix." yields the same 'phoenix', so the
    relative clause is not the cause.

D2  "I am also part owner of Omega AI LLC."  ->  occupation='part owner'
    An ownership role is filed as an occupation and its organization is
    dropped. The same fact phrased "I also own a company called Omega AI LLC"
    correctly yields business='omega ai llc' -- so the grammar, not the fact,
    decides whether Omega learns it. This also produced a second conflicting
    profile_occupation record alongside the real one (software_engineer).

D5b "Mitchell Ray is a co-owner of Omega AI LLC, he has 2 daughters, he's
    married and he also attends the University of Phoenix."  ->  NOTHING.
    Every copula path is guarded to a first-person or possessive subject, so a
    third-party statement extracts no facts at all. Live consequence: the
    operator's "remember this" committed an empty persona stub, discarded all
    four facts, and replied "not on record".
"""

from __future__ import annotations

import pytest

from memory.enrichment.personas import enrich


def _facets(text: str) -> dict:
    """facet -> set of extracted values."""
    out: dict[str, set] = {}
    for e in enrich(text).get("entities", []) or []:
        out.setdefault(str(e.get("facet")), set()).add(str(e.get("name", "")).lower())
    return out


def _subjects(text: str) -> dict:
    """facet -> subject the fact was attributed to."""
    return {
        str(e.get("facet")): str(e.get("subject") or "")
        for e in enrich(text).get("entities", []) or []
    }


# ── D1: education ────────────────────────────────────────────────────────
def test_institution_keeps_its_head_noun():
    """'University of Phoenix' is the institution, not the field 'phoenix'."""
    facets = _facets("I attend the University of Phoenix.")
    values = " ".join(v for values in facets.values() for v in values)
    assert "university of phoenix" in values, f"institution mangled: {facets}"


def test_bare_place_is_never_the_field_of_study():
    """The regression in one assertion: education must not be the bare place."""
    facets = _facets("I attend the University of Phoenix.")
    assert "phoenix" not in facets.get("education", set())


def test_degree_survives_extraction():
    """The credential is a separate proposition from enrollment."""
    facets = _facets(
        "I attend the University of Phoenix where I'm taking my Bachelors "
        "in Science in Cybersecurity."
    )
    blob = " ".join(v for values in facets.values() for v in values)
    assert "cybersecurity" in blob, f"degree/field dropped entirely: {facets}"


# ── D2: ownership role ───────────────────────────────────────────────────
def test_part_owner_is_not_an_occupation():
    facets = _facets("I am also part owner of Omega AI LLC.")
    assert "part owner" not in facets.get("occupation", set()), (
        f"ownership role filed as occupation: {facets}"
    )


def test_part_owner_keeps_its_organization():
    """The role fragment must carry its object -- 'part owner' alone is the
    same subjectless shape as the 'The creator.' record that broke recall."""
    facets = _facets("I am also part owner of Omega AI LLC.")
    blob = " ".join(v for values in facets.values() for v in values)
    assert "omega ai llc" in blob, f"organization dropped: {facets}"


def test_both_ownership_phrasings_agree():
    """Same fact, two grammars, same knowledge."""
    a = _facets("I am also part owner of Omega AI LLC.")
    b = _facets("I also own a company called Omega AI LLC.")
    blob_a = " ".join(v for values in a.values() for v in values)
    blob_b = " ".join(v for values in b.values() for v in values)
    assert "omega ai llc" in blob_a and "omega ai llc" in blob_b


def test_real_occupation_still_extracted():
    """Do not regress the genuine occupation path."""
    assert "software engineer" in " ".join(
        _facets("I am a software engineer.").get("occupation", set())
    )


# ── D5b: third-party subjects ────────────────────────────────────────────
MITCHELL = (
    "Mitchell Ray is a co-owner of Omega AI LLC, he has 2 daughters, "
    "he's married and he also attends the University of Phoenix."
)


def test_third_party_statement_extracts_facts_at_all():
    """The live failure: this sentence produced zero facts and an empty stub."""
    assert _facets(MITCHELL), "third-party statement extracted NOTHING"


@pytest.mark.parametrize(
    "needle", ["omega ai llc", "daughter", "married", "university of phoenix"]
)
def test_each_mitchell_fact_is_extracted(needle: str):
    blob = " ".join(v for values in _facets(MITCHELL).values() for v in values)
    assert needle in blob, f"lost {needle!r}; got {_facets(MITCHELL)}"


def test_every_facet_the_extractor_emits_is_registered_for_ingest():
    """A facet ingest does not know is dropped SILENTLY on the way to disk.

    Live 2026-07-31, first onboarding after day-zero: the extractor produced
    education_institution="the university of phoenix" -- exactly right -- and it
    never reached disk, because the facet was added to parse_extract without
    being registered in persona_ingest._SELF_FACETS. No error, no warning; the
    operator simply had no education record at all. This pins the two vocabularies
    together so the extractor can never again emit into a void.
    """
    from memory.persona_ingest import _FACET_SENTENCES, _SELF_FACETS

    corpus = [
        MITCHELL,
        "I have a son named Abel, I attend the University of Phoenix for my "
        "bachelors degree in Cybersecurity and I am also co-owner of Omega AI LLC.",
        "I am a software engineer and I live in DeRidder.",
        "I work at Omega AI and I love hiking.",
        "My sister is named Miranda and she is married.",
    ]
    emitted = {
        str(e.get("facet"))
        for text in corpus
        for e in enrich(text).get("entities", []) or []
        if e.get("facet")
    }
    # Relationship/name entities are routed by their own path, not via the
    # self-facet leaf writer.
    routed_elsewhere = {"relationship", "name"}
    unknown = emitted - routed_elsewhere - set(_SELF_FACETS)
    assert not unknown, f"extractor emits facets ingest silently drops: {sorted(unknown)}"

    unsentenced = (emitted - routed_elsewhere) - set(_FACET_SENTENCES)
    assert not unsentenced, f"facets with no sentence template: {sorted(unsentenced)}"


def test_third_party_facts_are_not_attributed_to_the_operator():
    """A fact about Mitchell must never be written as the operator's."""
    for subject in _subjects(MITCHELL).values():
        assert subject != "operator", "third-party fact attributed to the operator"


# ── live 2026-09-05: two ordinary statements, one lost and one mis-filed ────────
#
# "My favorite editor is vim."  ->  NOTHING (no preference verb, no copula rule)
# "My dog's name is Biscuit."   ->  name='biscuit' ON THE OPERATOR'S PROFILE


def test_favorite_copula_is_a_keyed_preference():
    facets = _facets("My favorite editor is vim.")
    assert facets.get("preference") == {"favorite editor: vim"}
    assert "name" not in facets


def test_preferred_copula_is_a_keyed_preference_too():
    assert _facets("My preferred shell is zsh.").get("preference") == {"preferred shell: zsh"}


def test_a_possessor_that_is_not_a_relation_still_owns_the_name():
    entities = enrich("My dog's name is Biscuit and she is a beagle.")["entities"]
    rel = next(e for e in entities if e["facet"] == "relationship")
    assert rel["name"] == "dog" and rel["linked_name"] == "biscuit"
    # The bare name entity is the relationship's COMPANION (its linked_name),
    # which persona ingest filters out of the operator's profile; it must never
    # be a standalone operator name.
    for entity in entities:
        if entity["facet"] == "name":
            assert entity["name"] == "biscuit"
            assert entity["name"] in {e.get("linked_name") for e in entities}


def test_the_operators_own_name_and_a_relations_name_are_untouched():
    assert _facets("My name is Noah.").get("name") == {"noah"}
    kim = enrich("My mother's name is Kim.")["entities"]
    rel = next(e for e in kim if e["facet"] == "relationship")
    assert rel["name"] == "mother" and rel["linked_name"] == "kim"
