from __future__ import annotations

from memory.enrichment.personas import enrich


def _entities(text: str) -> list[dict]:
    result = enrich(text)
    assert result is not None
    return result["entities"]


def _has_entity(
    entities: list[dict], *, facet: str, name: str, linked_name: str | None = None
) -> bool:
    for entity in entities:
        if entity.get("facet") != facet or entity.get("name") != name:
            continue
        if linked_name is not None and entity.get("linked_name") != linked_name:
            continue
        return True
    return False


def test_parses_compound_operator_workplace_and_named_sister():
    entities = _entities("My name is Noah, I work at X and I have a sister named Sheela.")

    assert _has_entity(entities, facet="name", name="noah")
    assert _has_entity(entities, facet="workplace", name="x")
    assert _has_entity(entities, facet="name", name="sheela")
    assert _has_entity(entities, facet="relationship", name="sister", linked_name="sheela")
    assert not any(
        entity.get("facet") in {"location", "workplace", "occupation"}
        and "sister named" in str(entity.get("name", ""))
        for entity in entities
    )


def test_parses_named_relationship_phrase():
    entities = _entities("I have a sister named Sheela.")

    assert _has_entity(entities, facet="name", name="sheela")
    assert _has_entity(entities, facet="relationship", name="sister", linked_name="sheela")


def test_parses_enumerated_multiple_named_relations():
    """Enumerated relations must ALL be captured — the second clause
    ('...and a son named Abel') has no 'I have'/'my' prefix of its own."""
    entities = _entities("I have a sister named Miranda, and a son named Abel.")

    assert _has_entity(entities, facet="name", name="miranda")
    assert _has_entity(entities, facet="relationship", name="sister", linked_name="miranda")
    assert _has_entity(entities, facet="name", name="abel")
    assert _has_entity(entities, facet="relationship", name="son", linked_name="abel")


def test_parses_education_field_of_study():
    entities = _entities("I go to college for cybersecurity.")

    assert _has_entity(entities, facet="education", name="cybersecurity")


def test_parses_education_direct_major_and_degree_forms():
    """The unambiguous forms must still resolve the field of study: 'major in X'
    directly, and an ambiguous verb paired with an institution/degree phrase."""
    assert _has_entity(
        _entities("I major in cybersecurity."), facet="education", name="cybersecurity"
    )
    assert _has_entity(
        _entities("I attend university for cybersecurity."),
        facet="education",
        name="cybersecurity",
    )
    assert _has_entity(
        _entities("I am pursuing a degree in cybersecurity."),
        facet="education",
        name="cybersecurity",
    )


def test_education_does_not_falsely_match_mundane_phrases():
    """`enrich()` now runs on EVERY operator chat turn and PERSISTS the result,
    so the education pattern must not fire on mundane 'go to X' / 'study X'
    phrases — otherwise 'I go to the store' durably mints education=the store."""
    for text in (
        "I go to the store.",
        "I go to sleep early.",
        "I study the problem every day.",
        "I go to the gym after work.",
    ):
        result = enrich(text)
        entities = (result or {}).get("entities", [])
        assert not any(e.get("facet") == "education" for e in entities), (
            f"education falsely matched on: {text!r} -> {entities!r}"
        )


def test_parses_operator_full_intro_statement():
    """The exact operator statement must decompose fully: self name +
    two distinct named relations + education field — no dropped facts."""
    entities = _entities(
        "I'm Noah, I have a sister named Miranda, a son named Abel, "
        "and I go to college for cybersecurity."
    )

    assert _has_entity(entities, facet="name", name="noah")
    assert _has_entity(entities, facet="name", name="miranda")
    assert _has_entity(entities, facet="relationship", name="sister", linked_name="miranda")
    assert _has_entity(entities, facet="name", name="abel")
    assert _has_entity(entities, facet="relationship", name="son", linked_name="abel")
    assert _has_entity(entities, facet="education", name="cybersecurity")
